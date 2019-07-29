use vm::errors::{UncheckedError, InterpreterResult as Result};
use vm::types::Value;
use vm::representations::{SymbolicExpression, SymbolicExpressionType};
use vm::{LocalContext, Environment, eval, apply, lookup_function};

pub fn list_cons(args: &[Value]) -> Result<Value> {
    Value::new_list(args)
}

pub fn list_filter(args: &[SymbolicExpression], env: &mut Environment, context: &LocalContext) -> Result<Value> {
    if args.len() != 2 {
        return Err(UncheckedError::InvalidArguments(format!("Wrong number of arguments ({}) to filter", args.len())).into())
    }
    let function_name = args[0].match_atom()
        .ok_or(UncheckedError::InvalidArguments("Filter must be called with a function name. We do not support eval'ing to functions.".to_string()))?;

    let function = lookup_function(&function_name, env)?;
    let list = eval(&args[1], env, context)?;
    if let Value::List(mut list_data) = list { 
        let mut output = Vec::new();
        for x in list_data.data.drain(..) {
            let argument = [ SymbolicExpression::atom_value(x.clone()) ];
            let filter_eval = apply(&function, &argument, env, context)?;
            if let Value::Bool(include) = filter_eval {
                if include {
                    output.push(x);
                } // else, filter out.
            } else {
                return Err(UncheckedError::TypeError("Bool".to_string(), filter_eval).into())
            }
        }
        Value::list_from(output)
    } else {
        Err(UncheckedError::TypeError("List".to_string(), list).into())
    }
}

pub fn list_fold(args: &[SymbolicExpression], env: &mut Environment, context: &LocalContext) -> Result<Value> {
    if args.len() != 3 {
        return Err(UncheckedError::InvalidArguments(format!("Wrong number of arguments ({}) to fold", args.len())).into())
    }
    let function_name = args[0].match_atom()
        .ok_or(UncheckedError::InvalidArguments("Fold must be called with a function name. We do not support eval'ing to functions.".to_string()))?;

    let function = lookup_function(&function_name, env)?;
    let list = eval(&args[1], env, context)?;
    let initial = eval(&args[2], env, context)?;
    if let Value::List(mut list_data) = list {
        list_data.data.drain(..).try_fold(
            initial,
            |acc, x| {
                let argument = [ SymbolicExpression::atom_value(x),
                                 SymbolicExpression::atom_value(acc) ];
                apply(&function, &argument, env, context)
            })
    } else {
        Err(UncheckedError::TypeError("List".to_string(), list).into())
    }
}

pub fn list_map(args: &[SymbolicExpression], env: &mut Environment, context: &LocalContext) -> Result<Value> {
    if args.len() != 2 {
        return Err(UncheckedError::InvalidArguments(format!("Wrong number of arguments ({}) to map", args.len())).into())
    }
    let function_name = args[0].match_atom()
        .ok_or(UncheckedError::InvalidArguments("Map must be called with a function name. We do not support eval'ing to functions.".to_string()))?;
    let function = lookup_function(&function_name, env)?;

    let list = eval(&args[1], env, context)?;
    if let Value::List(mut list_data) = list {
        let mapped_vec: Result<Vec<_>> = list_data.data.drain(..).map(|x| {
            let argument = [ SymbolicExpression::atom_value(x) ];
            apply(&function, &argument, env, context)
        }).collect();
        Value::list_from(mapped_vec?)
    } else {
        Err(UncheckedError::TypeError("List".to_string(), list).into())
    }
}