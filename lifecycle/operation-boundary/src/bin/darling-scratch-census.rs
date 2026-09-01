use darling_lifecycle_operation_boundary::scratch_collection::{
    collect, recover, CollectionRequest, RecoveryRequest,
};
use darling_lifecycle_operation_boundary::scratch_process_census::{
    census, duplicate_inherited_root, CensusRequest,
};
use serde::Deserialize;
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
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Request {
        Recovery(Box<RecoveryRequest>),
        Collection(Box<CollectionRequest>),
        Census(CensusRequest),
    }
    let (payload, output_limit) = match serde_json::from_slice(&input)? {
        Request::Collection(request) => (
            serde_json::to_vec(&collect(&request)?)?,
            request.output_limit_bytes,
        ),
        Request::Recovery(request) => (
            serde_json::to_vec(&recover(&request)?)?,
            request.output_limit_bytes,
        ),
        Request::Census(request) => {
            let root_fd = duplicate_inherited_root(request.root_fd)?;
            (
                serde_json::to_vec(&census(root_fd, &request)?)?,
                request.output_limit_bytes,
            )
        }
    };
    if payload.len() > output_limit {
        return Err("scratch census response exceeded output budget".into());
    }
    io::stdout().write_all(&payload)?;
    io::stdout().write_all(b"\n")?;
    Ok(())
}
