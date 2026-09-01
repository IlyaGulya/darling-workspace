use darling_lifecycle_operation_boundary::scratch_process_census::{
    census, duplicate_inherited_root, CensusRequest,
};
use std::io::{self, Read, Write};

fn main() {
    if let Err(error) = run() {
        eprintln!("darling-scratch-census: {error}");
        std::process::exit(2);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = Vec::new();
    io::stdin().take(65_537).read_to_end(&mut input)?;
    if input.len() > 65_536 {
        return Err("oversized scratch census request".into());
    }
    let request: CensusRequest = serde_json::from_slice(&input)?;
    let root_fd = duplicate_inherited_root(request.root_fd)?;
    let response = census(root_fd, &request)?;
    let payload = serde_json::to_vec(&response)?;
    if payload.len() > request.output_limit_bytes {
        return Err("scratch census response exceeded output budget".into());
    }
    io::stdout().write_all(&payload)?;
    io::stdout().write_all(b"\n")?;
    Ok(())
}
