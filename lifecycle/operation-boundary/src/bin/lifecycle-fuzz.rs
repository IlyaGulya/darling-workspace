use darling_lifecycle_operation_boundary::fuzz::{
    corpus_seed_hex, replay_program, run_smoke, safe_replay, verify_corpus,
    verify_kernel_observation, BytecodeOp, FuzzMode, KernelObservation, ScenarioBytecode,
    MAX_KERNEL_OBSERVATION_BYTES,
};
use serde_json::json;
use std::io::{self, Read};
use std::path::PathBuf;

fn hex_decode(value: &str) -> Result<Vec<u8>, String> {
    if !value.len().is_multiple_of(2) {
        return Err("hex input has odd length".to_string());
    }
    if value.len() > darling_lifecycle_operation_boundary::fuzz::MAX_INPUT_BYTES * 2 {
        return Err("hex input exceeds input limit".to_string());
    }
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len() / 2);
    for index in (0..bytes.len()).step_by(2) {
        let high = hex_nibble(bytes[index]).ok_or_else(|| "invalid hex input".to_string())?;
        let low = hex_nibble(bytes[index + 1]).ok_or_else(|| "invalid hex input".to_string())?;
        output.push((high << 4) | low);
    }
    Ok(output)
}

fn hex_nibble(value: u8) -> Option<u8> {
    match value {
        b'0'..=b'9' => Some(value - b'0'),
        b'a'..=b'f' => Some(value - b'a' + 10),
        b'A'..=b'F' => Some(value - b'A' + 10),
        _ => None,
    }
}

fn print_json<T: serde::Serialize>(value: &T) -> Result<(), Box<dyn std::error::Error>> {
    println!("{}", serde_json::to_string(value)?);
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("--verify-corpus") => {
            print_json(&verify_corpus()?)?;
        }
        Some("--smoke") => {
            let mut max_cases = 64usize;
            while let Some(argument) = args.next() {
                if argument == "--max-cases" {
                    max_cases = args.next().ok_or("--max-cases requires a value")?.parse()?;
                } else {
                    return Err(format!("unknown smoke argument: {argument}").into());
                }
            }
            print_json(&run_smoke(max_cases)?)?;
        }
        Some("--replay-hex") => {
            let value = args.next().ok_or("--replay-hex requires a value")?;
            let bytes = hex_decode(&value).map_err(io::Error::other)?;
            print_json(&safe_replay(&bytes))?;
        }
        Some("--verify-kernel-observation") => {
            let value = args
                .next()
                .ok_or("--verify-kernel-observation requires a trace")?;
            let bytes = hex_decode(&value).map_err(io::Error::other)?;
            let mut observation_bytes = Vec::new();
            io::stdin()
                .take(MAX_KERNEL_OBSERVATION_BYTES as u64 + 1)
                .read_to_end(&mut observation_bytes)?;
            if observation_bytes.len() > MAX_KERNEL_OBSERVATION_BYTES {
                return Err(io::Error::other("kernel observation exceeds input limit").into());
            }
            let observation: KernelObservation = serde_json::from_slice(&observation_bytes)?;
            print_json(
                &verify_kernel_observation(&bytes, &observation).map_err(io::Error::other)?,
            )?;
        }
        Some("--corpus-hex") => {
            let name = args.next().ok_or("--corpus-hex requires a seed name")?;
            println!("{}", corpus_seed_hex(&name)?);
        }
        Some("--materialize-corpus") => {
            let directory = PathBuf::from(
                args.next()
                    .ok_or("--materialize-corpus requires a directory")?,
            );
            let count = darling_lifecycle_operation_boundary::fuzz::materialize_corpus(&directory)?;
            print_json(&json!({
                "status": "CORPUS_MATERIALIZED",
                "count": count,
                "directory": directory,
            }))?;
        }
        Some("--explorer-case") => {
            let seed = args
                .next()
                .ok_or("--explorer-case requires seed")?
                .parse()?;
            let boundary = args
                .next()
                .ok_or("--explorer-case requires boundary")?
                .parse()?;
            let interleaving = args
                .next()
                .ok_or("--explorer-case requires interleaving")?
                .parse()?;
            let program = ScenarioBytecode {
                mode: FuzzMode::Explorer,
                seed,
                boundary,
                interleaving,
                initial_stable: 2,
                ops: vec![BytecodeOp::Noop],
            };
            print_json(&replay_program(&program)?)?;
        }
        Some("--decode-encode-contract") => {
            let malformed = [
                Vec::new(),
                vec![0u8; 1],
                vec![0u8; darling_lifecycle_operation_boundary::fuzz::MAX_INPUT_BYTES + 1],
            ];
            let rejected = malformed
                .iter()
                .filter(|input| {
                    darling_lifecycle_operation_boundary::fuzz::ScenarioBytecode::decode(input)
                        .is_err()
                })
                .count();
            print_json(&json!({
                "status": if rejected == malformed.len() { "PASS" } else { "FAIL" },
                "malformed_cases": malformed.len(),
                "rejected": rejected,
            }))?;
        }
        Some(argument) => return Err(format!("unknown command: {argument}").into()),
        None => {
            let mut input = Vec::new();
            io::stdin().read_to_end(&mut input)?;
            print_json(&safe_replay(&input))?
        }
    }
    Ok(())
}
