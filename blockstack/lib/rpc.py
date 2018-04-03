#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
    Blockstack
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016-2018 by Blockstack.org

    This file is part of Blockstack.

    Blockstack is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import sys
import errno
import time
import socket
import posixpath
import SocketServer
from SimpleHTTPServer import SimpleHTTPRequestHandler
import urllib
import urllib2
import re
import base64
import jsonschema
import urlparse
from jsonschema import ValidationError
import signal
import json
from decimal import Decimal

import client as blockstackd_client
from client import get_blockstackd_url
import scripts as blockstackd_scripts

import storage

from config import BLOCKSTACK_TEST, get_bitcoin_opts, get_blockstack_opts, get_blockstack_api_opts, LENGTHS, VERSION, RPC_MAX_ZONEFILE_LEN
from client import json_is_error, json_is_exception, decode_name_zonefile, create_bitcoind_service_proxy

import virtualchain
from virtualchain.lib.ecdsalib import get_pubkey_hex, verify_raw_data, ecdsa_private_key
from virtualchain import AuthServiceProxy, JSONRPCException

import blockstack_zones

log = virtualchain.get_logger()

JSONRPC_MAX_SIZE = 1024 * 1024

SATOSHIS_PER_COIN = 10**8

def format_unspents(unspents):
    """
    Used for testing only!
    """
    assert BLOCKSTACK_TEST, 'format_unspents can only be used in test mode!'
    return [{
        "transaction_hash": s["txid"],
        "outpoint": {
            'hash': s['txid'],
            'index': s["vout"],
        },
        "value": int(Decimal(s["amount"]*SATOSHIS_PER_COIN)),
        "out_script": s["scriptPubKey"],
        "confirmations": s["confirmations"]
        }
        for s in unspents
    ]


def get_unspents(address, bitcoind):
    """
    Used for testing only!

    Get the spendable transaction outputs, also known as UTXOs or
    unspent transaction outputs.

    NOTE: this will only return unspents if the address provided is present
    in the bitcoind server.
    """
    assert BLOCKSTACK_TEST, 'get_unspents can only be used in test mode!'

    addresses = [address]
    
    min_confirmations = 0
    max_confirmation = 2000000000  # just a very large number for max
    unspents = bitcoind.listunspent(min_confirmations, max_confirmation, addresses)

    if BLOCKSTACK_TEST and len(unspents) == 0:
        try:
            bitcoind.importaddress(str(address))
            unspents = bitcoind.listunspent(min_confirmations, max_confirmation, addresses)
        except Exception as e:
            return format_unspents([])

    return format_unspents(unspents)


class BlockstackAPIEndpointHandler(SimpleHTTPRequestHandler):
    '''
    Blockstack RESTful API endpoint.
    '''

    def _send_headers(self, status_code=200, content_type='application/json', more_headers={}):
        """
        Generate and reply headers
        """
        self.send_response(status_code)
        self.send_header('content-type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')    # CORS
        for (hdr, val) in more_headers.items():
            self.send_header(hdr, val)

        self.end_headers()


    def _reply_json(self, json_payload, status_code=200):
        """
        Return a JSON-serializable data structure
        """
        self._send_headers(status_code=status_code)
        json_str = json.dumps(json_payload)
        self.wfile.write(json_str)


    def _read_payload(self, maxlen=None):
        """
        Read raw uploaded data.
        Return the data on success
        Return None on I/O error, or if maxlen is not None and the number of bytes read is too big
        """

        client_address_str = "{}:{}".format(self.client_address[0], self.client_address[1])

        # check length
        read_len = self.headers.get('content-length', None)
        if read_len is None:
            log.error("No content-length given from {}".format(client_address_str))
            return None

        try:
            read_len = int(read_len)
        except:
            log.error("Invalid content-length")
            return None

        if maxlen is not None and read_len >= maxlen:
            log.error("Request from {} is too long ({} >= {})".format(client_address_str, read_len, maxlen))
            return None

        # get the payload
        request_str = self.rfile.read(read_len)
        return request_str


    def _read_json(self, schema=None, maxlen=JSONRPC_MAX_SIZE):
        """
        Read a JSON payload from the requester
        Return the parsed payload on success
        Return None on error
        """
        # JSON post?
        request_type = self.headers.get('content-type', None)
        client_address_str = "{}:{}".format(self.client_address[0], self.client_address[1])

        if request_type != 'application/json':
            log.error("Invalid request of type {} from {}".format(request_type, client_address_str))
            return None

        request_str = self._read_payload(maxlen=maxlen)
        if request_str is None:
            log.error("Failed to read request")
            return None

        # parse the payload
        request = None
        try:
            request = json.loads( request_str )
            if schema is not None:
                jsonschema.validate( request, schema )

        except ValidationError as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)
            log.error("Validation error on request {}...".format(
                request_str[:15]))
            if ve.validator == "maxLength":
                return {"error" : "maxLength"}

        except (TypeError, ValueError) as ve:
            if BLOCKSTACK_DEBUG:
                log.exception(ve)

            return None

        return request


    def parse_qs(self, qs):
        """
        Parse query string, but enforce one instance of each variable.
        Return a dict with the variables on success
        Return None on parse error
        """
        qs_state = urllib2.urlparse.parse_qs(qs)
        ret = {}
        for qs_var, qs_value_list in qs_state.items():
            if len(qs_value_list) > 1:
                return None

            ret[qs_var] = qs_value_list[0]

        return ret

    
    def get_path_and_qs(self):
        """
        Parse and obtain the path and query values.
        We don't care about fragments.

        Return {'path': ..., 'qs_values': ...} on success
        Return {'error': ...} on error
        """
        path_parts = self.path.split("?", 1)

        if len(path_parts) > 1:
            qs = path_parts[1].split("#", 1)[0]
        else:
            qs = ""

        path = path_parts[0].split("#", 1)[0]
        path = posixpath.normpath(urllib.unquote(path))

        qs_values = self.parse_qs( qs )
        if qs_values is None:
            return {'error': 'Failed to parse query string'}

        parts = path.strip('/').split('/')

        return {'path': path, 'qs_values': qs_values, 'parts': parts}


    def _route_match( self, method_name, path_info, route_table ):
        """
        Look up the method to call
        Return the route info and its arguments on success:
        Return None on error
        """
        path = path_info['path']

        for route_path, route_info in route_table.items():
            if method_name not in route_info['routes'].keys():
                continue

            grps = re.match(route_path, path)
            if grps is None:
                continue

            groups = grps.groups()
            return {
                'route': route_info,
                'method': route_info['routes'][method_name],
                'args': groups,
            }

        return None


    def OPTIONS_preflight( self, path_info ):
        """
        Give back CORS preflight check headers
        """
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')    # CORS
        self.send_header('Access-Control-Allow-Methods', 'GET, PUT, POST, DELETE')
        self.send_header('Access-Control-Allow-Headers', 'content-type, authorization, range')
        self.send_header('Access-Control-Expose-Headers', 'content-length, content-range')
        self.send_header('Access-Control-Max-Age', 21600)
        self.end_headers()
        return


    def GET_names_owned_by_address( self, path_info, blockchain, address ):
        """
        Get all names owned by an address
        Returns the list on success
        Return 401 on unsupported blockchain
        Returns 500 on failure to get names
        """
        if blockchain != 'bitcoin':
            return self._reply_json({'error': 'Invalid blockchain'}, status_code=401)

        blockstackd_url = get_blockstackd_url()
        address = str(address)

        subdomain_names = blockstackd_client.get_subdomains_owned_by_address(address, hostport=blockstackd_url)
        if json_is_error(subdomain_names):
            log.error("Failed to fetch subdomains owned by address")
            log.error(subdomain_names)
            subdomain_names = []
        
        # make sure we have the right encoding
        new_addr = virtualchain.address_reencode(address)
        if new_addr != address:
            log.debug("Re-encode {} to {}".format(new_addr, address))
            address = new_addr
        
        res = blockstackd_client.get_names_owned_by_address(address, hostport=blockstackd_url)
        if json_is_error(res):
            log.error("Failed to get names owned by address")
            self._reply_json({'error': 'Failed to list names by address'}, status_code=500)
            return

        self._reply_json({'names': res + subdomain_names})
        return

    
    def GET_account_tokens(self, path_info, account_addr):
        """
        Get all token types that an address owns
        Returns {'tokens': [...]}
        """
        blockstackd_url = get_blockstackd_url()
        res = blockstackd_client.get_account_tokens(account_addr, hostport=blockstackd_url)
        if json_is_error(res):
            log.error("Failed to load tokens for {}: {}".format(account_addr, res['error']))
            return self._reply_json({'error': 'Failed to load tokens for {}: {}'.format(account_addr, res['error'])}, status_code=500)

        self._reply_json({'tokens': res})
        return 


    def GET_account_balance(self, path_info, account_addr, token_type):
        """
        Get the balance of a particular token
        Returns {'balance': ...}
        """
        blockstackd_url = get_blockstackd_url()
        res = blockstackd_client.get_account_balance(account_addr, token_type, hostport=blockstackd_url)
        if json_is_error(res):
            log.error("Failed to get account balance for {} {}: {}".format(account_addr, token_type, res['error']))
            return self._reply_json({'error': 'Failed to get balance of {} for {}: {}'.format(tokentype, account_addr, res['error'])}, status_code=500)

        self._reply_json({'balance': str(res)})     # NOTE: use a string, since this can be too big for js clients to parse
        return


    def GET_names( self, path_info ):
        """
        Get all names in existence
        If `all=true` is set, then include expired names.
        Returns the list on success
        Returns 401 on invalid arguments
        Returns 500 on failure to get names
        """

        include_expired = False

        qs_values = path_info['qs_values']
        page = qs_values.get('page', None)
        if page is None:
            log.error("Page required")
            return self._reply_json({'error': 'page= argument required'}, status_code=401)

        try:
            page = int(page)
        except ValueError:
            log.error("Invalid page")
            return self._reply_json({'error': 'Invalid page= value'}, status_code=401)

        if qs_values.get('all', '').lower() in ['1', 'true']:
            include_expired = True

        offset = page * 100
        count = 100

        blockstackd_url = get_blockstackd_url()
        res = blockstackd_client.get_all_names(offset, count, include_expired=include_expired, hostport=blockstackd_url)
        if json_is_error(res):
            log.error("Failed to list all names (offset={}, count={}): {}".format(offset, count, res['error']))
            self._reply_json({'error': 'Failed to list all names'}, status_code=500)
            return

        self._reply_json(res)
        return


    def GET_name_info( self, path_info, name ):
        """
        Look up a name's zonefile, address, and last TXID
        Reply status, zonefile, zonefile hash, address, and last TXID.
        'status' can be 'available', 'registered', 'revoked', or 'pending'
        """
        blockstackd_url = get_blockstackd_url()

        name_rec = None
        try:
            name_rec = blockstackd_client.get_name_record(name, include_history=False, hostport=blockstackd_url)
        except:
            return self._reply_json({'error': 'Failed to connect to blockstack daemon'}, status_code=500)

        if 'error' in name_rec:
            if 'not found' in name_rec['error'].lower():
                return self._reply_json({'status': 'available'}, status_code=404)
            elif 'failed to load subdomain' in name_rec['error'].lower():
                return self._reply_json({'status': 'available'}, status_code=404)
            else:
                return self._reply_json({'error': 'Blockstack daemon error: {}'.format(name_rec['error'])}, status_code=500)

        zonefile_txt = None

        if 'zonefile' in name_rec:
            zonefile_txt = base64.b64decode(name_rec['zonefile'])
            res = decode_name_zonefile(name, zonefile_txt)
            if res is None:
                log.error("Failed to parse zone file for {}".format(name))
                zonefile_txt = {'error': 'Non-standard zone file'}

        ret = {}

        if blockstackd_scripts.is_subdomain(name):
            # subdomain
            log.debug("{} is registered_subdomain".format(name))
            ret = {
                'status': 'registered_subdomain',
                'zonefile': zonefile_txt,
                'zonefile_hash': storage.get_zonefile_data_hash(zonefile_txt),
                'address': name_rec['address'],
                'blockchain': 'bitcoin',
                'last_txid': name_rec['txid']
            }

        else:
            status = 'revoked' if name_rec['revoked'] else 'registered'
            address = name_rec['address']
            if address:
                address = virtualchain.address_reencode(str(address), network='mainnet')

            log.debug("{} is {}".format(name, status))
            ret = {
                'status': status,
                'zonefile': zonefile_txt,
                'zonefile_hash': name_rec['value_hash'],
                'address': address,
                'last_txid': name_rec['txid'],
                'blockchain': 'bitcoin',
                'expire_block': name_rec['expire_block'],
            }

        return self._reply_json(ret)


    def GET_name_history(self, path_info, name ):
        """
        Get the history of a name or subdomain.
        Takes `start_block` and `end_block` in the query string.
        return the history on success
        return 401 on invalid start_block or end_block
        return 500 on failure to query blockstack server
        """
        qs_values = path_info['qs_values']
        start_block = qs_values.get('start_block', None)
        end_block = qs_values.get('end_block', None)

        try:
            if start_block is None:
                start_block = FIRST_BLOCK_MAINNET
            else:
                start_block = int(start_block)

            if end_block is None:
                end_block = 2**32   # hope we never get this many blocks!
            else:
                end_block = int(end_block)
        except:
            log.error("Invalid start_block or end_block")
            self._reply_json({'error': 'Invalid start_block or end_block'}, status_code=401)
            return

        blockstackd_url = get_blockstackd_url()
        res = blockstackd_client.get_name_record(name, include_expired=True, include_history=True, hostport=blockstackd_url)
        if json_is_error(res):
            self._reply_json({'error': res['error']}, status_code=500)
            return

        history = {}
        for block_height in res['history'].keys():
            if int(block_height) >= start_block and int(block_height) <= end_block:
                history[block_height] = res['history'][block_height]

        self._reply_json(history)
        return


    def GET_name_zonefile( self, path_info, name ):
        """
        Get the name's current zonefile data.
        With `raw=1` on the query string, return the raw zone file.
        Otherwise, return the (raw) zone file only if it
        is well-formed (and return it in a JSON dict).

        Reply the {'zonefile': zonefile} on success
        Reply 500 on failure to fetch or parse data
        """
        raw = path_info['qs_values'].get('raw', '')
        raw = (raw.lower() in ['1', 'true'])
        
        blockstackd_url = get_blockstackd_url()
        resp = blockstackd_client.get_name_record(name, include_history=False, hostport=blockstackd_url)
        if json_is_error(resp):
            log.error("Failed to load zone file for {}: {}".format(name, resp['error']))
            return self._reply_json({"error": resp['error']}, status_code=500)

        if 'zonefile' not in resp or resp['zonefile'] is None:
            log.error("No zone file for {}".format(name))
            return self._reply_json({'error': 'No zone file for name'}, status_code=404)

        try:
            zonefile_txt = base64.b64decode(resp['zonefile'])
        except:
            log.error("Zone file data is not serialized properly")
            return self._reply_json({'error': 'Zone file is not serialized properly'}, status_code=401)

        if raw:
            self._send_headers(status_code=200, content_type='application/octet-stream')
            self.wfile.write(zonefile_txt)
            return

        else:
            res = decode_name_zonefile(name, zonefile_txt)
            if res is None:
                log.error("Failed to parse zone file for {}".format(name))
                return self._reply_json({'error': 'Non-standard zone file.  Try passing raw=1 to get the raw zone file.'}) 

            # successfully decodes.  Safe to return as a JSON object.
            return self._reply_json({'zonefile': zonefile_txt})


    def GET_zonefile(self, path_info, zonefile_hash):
        """
        Get a raw zonefile
        Reply the zonefile as application/octet-stream
        Reply 404 if not found
        """
        blockstackd_url = get_blockstackd_url()
        resp = blockstackd_client.get_zonefiles(blockstackd_url, [str(zonefile_hash)])
        if json_is_error(resp):
            log.error("Failed to get {}: {}".format(zonefile_hash, resp['error']))
            return self._reply_json({'error': resp['error']}, status_code=500)

        if str(zonefile_hash) not in resp['zonefiles']:
            return self._reply_json({'error': 'Blockstack node does not have this zonefile.  Try again later.'}, status_code=404)

        self._send_headers(status_code=200, content_type='application/octet-stream')
        self.wfile.write(resp['zonefiles'][str(zonefile_hash)])
        return
        
    
    def PUT_zonefile(self, path_info):
        """
        Put a zonefile
        Reply 200 on success
        Reply 202 if the upstream node already has the zonefile
        Reply 400 if the node refused to save this zonefile
        Reply 404 if the zonefile hash is not found
        Reply 500 if the upstream node didn't accept it
        """
        blockstackd_url = get_blockstackd_url()
        zonefile_data = self._read_payload(maxlen=RPC_MAX_ZONEFILE_LEN)
        if zonefile_data is None:
            # too long
            return self._reply_json({'error': 'zone file is too long'}, status_code=401)
            
        zonefile_hash = storage.get_zonefile_data_hash(zonefile_data)
        zonefiles_b64 = [base64.b64encode(str(zonefile_data))]
        resp = blockstackd_client.put_zonefiles(blockstackd_url, zonefiles_b64)
        if json_is_error(resp):
            log.error("Failed to put {}: {}".format(zonefile_hash, resp['error']))
            return self._reply_json({'error': resp['error']}, status_code=500)

        if len(resp['saved']) != 1:
            log.error("Did not save {} saved is {}".format(zonefile_hash, resp['saved']))
            return self._reply_json({'error': 'Blockstack node did not save zonefile {}'}, status_code=400)

        if resp['saved'][0] == 0:
            # accepted but not processed
            return self._send_headers(status_code=202, content_type='application/octet-stream')

        else:
            # accepted and saved
            return self._send_headers(status_code=200, content_type='application/octet-stream')


    def get_name_zonefile_hashes(self, name):
        """
        List all zonefile hashes of a name, in historic order.
        Return a list of hashes on success.
        Return {'error': ...} on failure
        """
        blockstack_hostport = get_blockstackd_url()
        name_rec = blockstackd_client.get_name_record(name, include_history=True, hostport=blockstack_hostport)
        if 'error' in name_rec:
            log.error("Failed to get name record for {}: {}".format(name, name_rec['error']))
            return {'error': 'Failed to get name record for {}: {}'.format(name, name_rec['error'])}

        name_history = name_rec['history']
        all_update_hashes = []

        block_ids = name_history.keys()
        block_ids.sort()
        for block_id in block_ids:
            history_items = name_history[block_id]
            for history_item in history_items:
                value_hash = history_item.get('value_hash', None)
                if value_hash is None:
                    continue

                if len(all_update_hashes) > 0 and all_update_hashes[-1] == value_hash:
                    continue

                # changed
                all_update_hashes.append(value_hash)

        return all_update_hashes


    def GET_name_zonefile_by_hash( self, path_info, name, zonefile_hash ):
        """
        Get a historic zonefile for a name
        With `raw=1` on the query string, return the raw zone file

        Reply 200 with {'zonefile': zonefile} on success
        Reply 204 with {'error': ...} if the zone file is non-standard
        Reply 404 on not found
        Reply 500 on failure to fetch data
        """
        raw = path_info['qs_values'].get('raw', '')
        raw = (raw.lower() in ['1', 'true'])

        blockstack_hostport = get_blockstackd_url()

        # historic_zonefiles = data.list_update_history(name)
        historic_zonefiles = self.get_name_zonefile_hashes(name)
        if json_is_error(historic_zonefiles):
            self._reply_json({'error': historic_zonefiles['error']}, status_code=500)
            return

        if zonefile_hash not in historic_zonefiles:
            self._reply_json({'error': 'No such zonefile'}, status_code=404)
            return

        resp = blockstackd_client.get_zonefiles(blockstack_hostport, [str(zonefile_hash)])
        if json_is_error(resp):
            self._reply_json({'error': resp['error']}, status_code=500)
            return

        if str(zonefile_hash) not in resp['zonefiles']:
            return self._reply_json({'error': 'Blockstack does not have this zonefile.  Try again later.'}, status_code=404)

        if raw:
            self._send_headers(status_code=200, content_type='application/octet-stream')
            self.wfile.write(resp['zonefiles'][str(zonefile_hash)])

        else:
            # make sure it's valid
            if str(zonefile_hash) not in resp['zonefiles']:
                log.debug('Failed to find zonefile hash {}, possess {}'.format(
                    str(zonefile_hash), resp['zonefiles'].keys()))
                return self._reply_json({'error': 'No such zonefile'}, status_code=404)

            zonefile_txt = resp['zonefiles'][str(zonefile_hash)]
            res = decode_name_zonefile(name, zonefile_txt)
            if res is None:
                log.error("Failed to parse zone file for {}".format(name))
                self._reply_json({'error': 'Non-standard zone file for {}'.format(name)}, status_code=204)
                return

            self._reply_json({'zonefile': zonefile_txt})

        return


    def GET_user_profile( self, path_info, user_id ):
        """
        Get a user profile.
        Reply the profile on success
        Return 404 on failure to load
        """

        blockstackd_url = get_blockstackd_url()
        resp = blockstackd_client.resolve_profile(user_id, hostport=blockstackd_url)
        if json_is_error(resp):
            self._reply_json({'error': resp['error']}, status_code=404)
            return

        self._reply_json(resp['profile'])
        return


    def GET_prices_namespace( self, path_info, namespace_id ):
        """
        Get the price for a namespace
        Reply the price for the namespace as {'satoshis': price in satoshis}
        Reply 500 if we can't reach the namespace for whatever reason
        """
        blockstackd_url = get_blockstackd_url()
        price_info = blockstackd_client.get_namespace_cost(namespace_id, hostport=blockstackd_url)
        if json_is_error(price_info):
            # error
            status_code = None
            if json_is_exception(price_info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': price_info['error']}, status_code=status_code)
            return

        ret = {
            'amount': str(price_info['amount']),        # helps JS clients that can't parse big ints
            'units': price_info['units'],
        }
        if 'satoshis' in price_info:
            ret['satoshis'] = price_info['satoshis']

        return self._reply_json(ret)


    def GET_prices_name( self, path_info, name ):
        """
        Get the price for a name in a namespace
        Reply the price as {'name_price': {'amount': str(...), 'units': str(...)}} (also, 'satoshis': ... if the name is in BT)
        Reply 404 if the namespace doesn't exist
        Reply 500 if we can't reach the server for whatever reason
        """
        blockstackd_url = get_blockstackd_url()
        price_info = blockstackd_client.get_name_cost(name, hostport=blockstackd_url)
        if json_is_error(price_info):
            # error
            status_code = None
            if json_is_exception(price_info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': price_info['error']}, status_code=status_code)
            return

        ret = {
            'amount': str(price_info['amount']),        # helps JS clients that can't parse big ints
            'units': price_info['units'],
        }
        if 'satoshis' in price_info:
            ret['satoshis'] = price_info['satoshis']

        return self._reply_json({'name_price': ret})


    def GET_namespaces( self, path_info ):
        """
        Get the list of all namespaces
        Reply all existing namespaces
        Reply 500 if we can't reach the server for whatever reason
        """

        qs_values = path_info['qs_values']
        offset = qs_values.get('offset', None)
        count = qs_values.get('count', None)

        blockstackd_url = get_blockstackd_url()
        namespaces = blockstackd_client.get_all_namespaces(offset=offset, count=count, hostport=blockstackd_url)
        if json_is_error(namespaces):
            # error
            status_code = None
            if json_is_exception(namespaces):
                status_code = 500
            else:
                status_code = 404

            return self._reply_json({'error': namespaces['error']}, status_code=500)

        self._reply_json(namespaces)
        return


    def GET_namespace_info( self, path_info, namespace_id ):
        """
        Look up a namespace's info
        Reply information about a namespace
        Reply 404 if the namespace doesn't exist
        Reply 500 for any error in talking to the blocksatck server
        """
        blockstackd_url = get_blockstackd_url()
        namespace_rec = blockstackd_client.get_namespace_record(namespace_id, hostport=blockstackd_url)
        if json_is_error(namespace_rec):
            # error
            status_code = None
            if json_is_exception(namespace_rec):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': namespace_rec['error']}, status_code=status_code)
            return

        self._reply_json(namespace_rec)
        return


    def GET_namespace_num_names(self, path_info, namespace_id):
        """
        Get the number of names in a namespace
        Reply the number on success
        Reply 404 if the namespace does not exist
        Reply 500 on failure to talk to the blockstack server
        """
        blockstackd_url = get_blockstackd_url()
        name_count = get_num_names_in_namespace(namespace_id, hostport=blockstackd_url)
        if json_is_error(name_count):
            log.error("Failed to load namespace count for {}: {}".format(namespace_id, name_count['error']))
            return self._reply_json({'error': 'Failed to load namespace count: {}'.format(name_count['error'])}, status_code=404)

        return self._reply_json(name_count)


    def GET_namespace_names( self, path_info, namespace_id ):
        """
        Get the list of names in a namespace
        Reply the list of names in a namespace
        Reply 404 if the namespace doesn't exist
        Reply 500 for any error in talking to the blockstack server
        """
        qs_values = path_info['qs_values']
        page = qs_values.get('page', None)
        if page is None:
            log.error("Page required")
            return self._reply_json({'error': 'page= argument required'}, status_code=401)

        try:
            page = int(page)
        except ValueError:
            log.error("Invalid page")
            return self._reply_json({'error': 'Invalid page= value'}, status_code=401)

        offset = page * 100
        count = 100

        blockstackd_url = get_blockstackd_url()
        namespace_names = blockstackd_client.get_names_in_namespace(namespace_id, offset=offset, count=count, hostport=blockstackd_url)
        if json_is_error(namespace_names):
            # error
            status_code = None
            if json_is_exception(namespace_names):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': namespace_names['error']}, status_code=status_code)
            return

        self._reply_json(namespace_names)
        return


    def GET_blockchain_ops( self, path_info, blockchain_name, blockheight ):
        """
        Get the name's historic name operations
        Reply the list of nameops at the given block height
        Reply 404 for blockchains other than those supported
        Reply 500 for any error we have in talking to the blockstack server
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return
        
        blockstackd_url = get_blockstackd_url()
        nameops = blockstackd_client.get_blockstack_transactions_at(int(blockheight), hostport=blockstackd_url)
        if json_is_error(nameops):
            # error
            status_code = None
            if json_is_exception(nameops):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': nameops['error']}, status_code=status_code)
            return

        self._reply_json(nameops)
        return


    def GET_blockchain_name_record( self, path_info, blockchain_name, name ):
        """
        Get the name's blockchain record in full
        Reply the raw blockchain record on success
        Reply 404 if the name is not found
        Reply 500 if we have an error talking to the server
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        blockstackd_url = get_blockstackd_url()
        name_rec = blockstackd_client.get_name_record(name, include_history=False, hostport=blockstackd_url)
        if json_is_error(name_rec):
            # error
            status_code = None
            if json_is_exception(name_rec):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': name_rec['error']}, status_code=status_code)
            return

        return self._reply_json(name_rec)


    def GET_blockchain_num_names( self, path_info, blockchain_name ):
        """
        Handle GET /blockchains/:blockchainID/name_count
        Takes `all=true` to include expired names
        Reply with the number of names on this blockchain
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        include_expired = False
        
        qs_values = path_info['qs_values']
        if qs_values.get('all', '').lower() in ['1', 'true']:
            include_expired = True

        blockstackd_url = get_blockstackd_url()
        num_names = blockstackd_client.get_num_names(include_expired=include_expired, hostport=blockstackd_url)
        if json_is_error(num_names):
            if json_is_exception(info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': info['error']}, status_code=status_code)
            return

        self._reply_json({'names_count': num_names})
        return


    def GET_blockchain_consensus( self, path_info, blockchain_name ):
        """
        Handle GET /blockchain/:blockchainID/consensus
        Reply the consensus hash at this blockchain's tip
        Reply 401 for unrecognized blockchain
        Reply 404 for blockchains that we don't support
        Reply 500 for any error we have in talking to the blockstack server
        """
        if blockchain_name != 'bitcoin':
            # not supported
            self._reply_json({'error': 'Unsupported blockchain'}, status_code=401)
            return

        blockstackd_url = get_blockstackd_url()
        info = blockstackd_client.getinfo(hostport=blockstackd_url)
        if json_is_error(info):
            # error
            status_code = None
            if json_is_exception(info):
                status_code = 500
            else:
                status_code = 404

            self._reply_json({'error': info['error']}, status_code=status_code)
            return

        self._reply_json({'consensus_hash': info['consensus']})
        return


    def _get_balance( self, get_address, min_confs ):
        """
        Works only in test mode!
        Get the confirmed balance for an address
        """
        bitcoind_opts = get_bitcoin_opts()
        bitcoind_host = bitcoind_opts['bitcoind_server']
        bitcoind_port = bitcoind_opts['bitcoind_port']
        bitcoind_user = bitcoind_opts['bitcoind_user']
        bitcoind_passwd = bitcoind_opts['bitcoind_passwd']

        bitcoind = create_bitcoind_service_proxy(bitcoind_user, bitcoind_passwd, server=bitcoind_host, port=bitcoind_port)
        address = virtualchain.address_reencode(get_address)

        try:
            unspents = get_unspents(address, bitcoind)
        except Exception as e:
            log.exception(e)
            return {'error': 'Failed to get unspents for {}'.format(get_address)}

        satoshis_confirmed = sum(confirmed_utxo['value'] for confirmed_utxo in 
                                 filter(lambda utxo: utxo['confirmations'] >= min_confs, unspents))

        return {'balance': satoshis_confirmed}


    def GET_confirmed_balance_insight( self, path_info, address ):
        """
        Works only in test mode!
        Handle GET /insight-api/addr/:address/balance
        """
        if not BLOCKSTACK_TEST:
            return self._send_headers(status_code=404, content_type='text/plain')

        res = self._get_balance(address, 1)
        if 'error' in res:
            return self._reply_json(res, status_code=500)

        return self._reply_json(res['balance'])


    def GET_unconfirmed_balance_insight( self, path_info, address ):
        """
        Handle GET /insight-api/addr/:address/unconfirmedBalance
        """
        if not BLOCKSTACK_TEST:
            return self._send_headers(status_code=404, content_type='text/plain')

        res = self._get_balance(address, 0)
        if 'error' in res:
            return self._reply_json(res, status_code=500)

        return self._reply_json(res['balance'])


    def GET_utxos_insight( self, path_info, address ):
        """
        Handle GET /insight-api/addr/:address/utxo
        NOTE: this is not compatible with the Bitcore Insight API method of the same name
        """
        if not BLOCKSTACK_TEST:
            return self._send_headers(status_code=404, content_type='text/plain')

        bitcoind_opts = get_bitcoin_opts()
        bitcoind_host = bitcoind_opts['bitcoind_server']
        bitcoind_port = bitcoind_opts['bitcoind_port']
        bitcoind_user = bitcoind_opts['bitcoind_user']
        bitcoind_passwd = bitcoind_opts['bitcoind_passwd']

        bitcoind = create_bitcoind_service_proxy(bitcoind_user, bitcoind_passwd, server=bitcoind_host, port=bitcoind_port)
        address = virtualchain.address_reencode(get_address)
        utxos = get_unspents(address, bitcoind)
        return self._reply_json(utxos)


    def GET_ping(self, path_info):
        """
        ping
        """
        self._reply_json({'status': 'alive', 'version': VERSION})
        return


    def _dispatch(self, method_name):
        """
        Top-level dispatch method
        """

        URLENCODING_CLASS = r'[a-zA-Z0-9\-_.~%]+'
        NAME_CLASS = r'[a-z0-9\-_.+]{{{},{}}}'.format(3, LENGTHS['blockchain_id_name'])
        NAMESPACE_CLASS = r'[a-z0-9\-_+]{{{},{}}}'.format(1, LENGTHS['namespace_id'])
        BASE58CHECK_CLASS = r'[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+'

        routes = {
            r'^/v1/ping$': {
                'routes': {
                    'GET': self.GET_ping,
                },
            },
            r'^/v1/addresses/({})/({})$'.format(URLENCODING_CLASS, BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_names_owned_by_address,
                },
            },
            r'^/v1/accounts/({})/tokens$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_account_tokens,
                },
            },
            r'^/v1/accounts/({})/(.+){{1,19}}/balance$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_account_balance,
                },
            },
            r'^/v1/blockchains/({})/name_count'.format(URLENCODING_CLASS) : {
                'routes': {
                    'GET': self.GET_blockchain_num_names
                },
            },
            r'^/v1/blockchains/({})/operations/([0-9]+)$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_ops
                },
            },
            r'^/v1/blockchains/({})/names/({})$'.format(URLENCODING_CLASS, NAME_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_name_record,
                },
            },
            r'^/v1/blockchains/({})/names/({})/history$'.format(URLENCODING_CLASS, NAME_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_name_history
                },
            },
            r'^/v1/blockchains/({})/consensus$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_blockchain_consensus,
                },
            },
            r'^/v1/names$': {
                'routes': {
                    'GET': self.GET_names,
                },
            },
            r'^/v1/names/({})$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_info,
                },
            },
            r'^/v1/names/({})/history$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_history,
                },
            },
            r'^/v1/names/({})/zonefile$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_name_zonefile,
                },
            },
            r'^/v1/names/({})/zonefile/([0-9a-fA-F]{{{}}})$'.format(NAME_CLASS, LENGTHS['value_hash']): {
                'routes': {
                    'GET': self.GET_name_zonefile_by_hash,     # returns a zonefile
                },
            },
            r'^/v1/namespaces$': {
                'routes': {
                    'GET': self.GET_namespaces,
                },
            },
            r'^/v1/namespaces/({})$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_namespace_info,
                },
            },
            r'^/v1/namespaces/({})/names$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_namespace_names,
                },
            },
            r'^/v1/namespaces/({})/name_count$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_namespace_num_names,
                },
            },
            r'^/v1/node/ping$': {
                'routes': {
                    'GET': self.GET_ping,
                },
            },
            r'^/v1/prices/namespaces/({})$'.format(NAMESPACE_CLASS): {
                'routes': {
                    'GET': self.GET_prices_namespace,
                },
            },
            r'^/v1/prices/names/({})$'.format(NAME_CLASS): {
                'routes': {
                    'GET': self.GET_prices_name,
                },
            },
            r'^/v1/users/({})$'.format(URLENCODING_CLASS): {
                'routes': {
                    'GET': self.GET_user_profile,
                },
            },
            r'^/insight-api/addr/({})/balance$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_confirmed_balance_insight,
                },
            },
            r'^/insight-api/addr/({})/unconfirmedBalance$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_unconfirmed_balance_insight,
                },
            },
            r'^/insight-api/addr/({})/utxo$'.format(BASE58CHECK_CLASS): {
                'routes': {
                    'GET': self.GET_utxos_insight,
                },
            },
            r'^/v1/zonefiles/([0-9a-fA-F]{{{}}})$'.format(LENGTHS['value_hash']*2): {
                'routes': {
                    'GET': self.GET_zonefile,
                },
            },
            r'^/v1/zonefiles$': {
                'routes': {
                    'PUT': self.PUT_zonefile,
                },
            },
            r'^/v1/.*$': {
                'routes': {
                    'OPTIONS': self.OPTIONS_preflight,
                },
            },
        }
        
        conf = get_blockstack_api_opts()
        if not conf['enabled']:
            # this feature is not enabled
            self._send_headers(status_code=404, content_type='text/plain')
            return 

        path_info = self.get_path_and_qs()
        if 'error' in path_info:
            self._send_headers(status_code=401, content_type='text/plain')
            return

        qs_values = path_info['qs_values']

        route_info = self._route_match( method_name, path_info, routes )
        if route_info is None:
            log.debug("Unmatched route: {} '{}'".format(method_name, path_info['path']))
            log.debug(json.dumps( routes.keys(), sort_keys=True, indent=4 ))
            self._send_headers(status_code=404, content_type='text/plain')
            return

        route_args = route_info['args']
        route_method = route_info['method']
        route = route_info['route']

        log.debug("\nfull path: {}\nmethod: {}\npath: {}\nqs: {}\nheaders:\n{}\n".format(self.path, method_name, path_info['path'], qs_values, '\n'.join( '{}: {}'.format(k, v) for (k, v) in self.headers.items() )))

        try:
            return route_method( path_info, *route_args )
        except Exception as e:
            log.exception(e)
            return self._send_headers(status_code=500, content_type='text/plain')


    def do_GET(self):
        """
        Top-level GET dispatch
        """
        return self._dispatch("GET")

    def do_POST(self):
        """
        Top-level POST dispatch
        """
        return self._dispatch("POST")

    def do_PUT(self):
        """
        Top-level PUT dispatch
        """
        return self._dispatch("PUT")

    def do_DELETE(self):
        """
        Top-level DELETE dispatch
        """
        return self._dispatch("DELETE")

    def do_HEAD(self):
        """
        Top-level HEAD dispatch
        """
        return self._dispatch("HEAD")

    def do_OPTIONS(self):
        """
        Top-level OPTIONS dispatch
        """
        return self._dispatch("OPTIONS")

    def do_PATCH(self):
        """
        TOp-level PATCH dispatch
        """
        return self._dispatch("PATCH")


class BlockstackAPIEndpoint(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    """
    Lightweight API endpoint to Blockstack server:
    exposes all of the client methods via a RESTful interface,
    so other local programs (e.g. those that can't use the library)
    can access Blockstack functionality.
    """

    def __init__(self, host=None, port=None,
                 handler=BlockstackAPIEndpointHandler):

        """
        """
        SocketServer.TCPServer.__init__(self, (host, port), handler, bind_and_activate=False)

        conf = get_blockstack_api_opts()
        assert conf is not None, 'No API server config given.  Add a [blockstack-api] section to your blockstack-server.ini'
        assert conf['enabled'], 'API server is disabled.  Set "enabled=True" in the [blockstack-api] section of your blockstack-server.ini'

        if host is None:
            host = conf['api_host']

        if port is None:
            port = conf['api_port']

        self.host = host
        self.port = port


    def bind(self):
        """
        Bind to our port
        """
        log.debug("Set SO_REUSADDR")
        self.socket.setsockopt( socket.SOL_SOCKET, socket.SO_REUSEADDR, 1 )
        
        # we want daemon threads, so we join on abrupt shutdown (applies if multithreaded) 
        self.daemon_threads = True

        self.server_bind()
        self.server_activate()

