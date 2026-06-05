# Bio-MCP Server

Bio-MCP Server is a Dockerized bioinformatics MCP server that runs without
Bioconda. It exposes native command-line tools, Java JARs, and Python/R scripts
to MCP clients such as Codex. Tools are declared in YAML, registered at startup,
and can also be installed and registered dynamically by an agent at runtime.

The server is designed for large scientific outputs: it previews small results,
persists multi-gigabyte text outputs under `/data/mcp_outputs`, and can return
images, Markdown data tables, and PDF text summaries as MCP content.

## Features

- No Bioconda: uses `python:3.11-slim-bookworm`, apt packages, and pip wheels.
- Dynamic registry: built-in tools are loaded from `/app/registry.yaml`.
- Persistent user tools: agent-added tools are stored in `/data/mcp_registry.yaml`.
- Persistent downloaded binaries: `binary_url` installs to `/data/mcp_tools/bin`.
- Secure execution: commands are split with `shlex.split`; `shell=True` is not used.
- Data firewall: path inputs must resolve inside `/data`.
- Large-output interception: full raw output is written to `/data/mcp_outputs`.
- Multimodal outputs: image, dataframe, PDF, and text rendering are supported.
- MCP transports: supports both `stdio` and `streamable-http`.
- Shared server mode: one HTTP MCP server can be used by multiple agents.

## Built-In Tools

The baseline `registry.yaml` includes examples such as:

- `samtools_flagstat`
- `bedtools_intersect`
- `blastn_tabular`
- `fastp_qc`
- `render_qc_png`
- `extract_pdf_summary`
- `picard_collect_alignment_summary`

The server also exposes management tools:

- `install_bio_tool`
- `append_tool_to_registry`
- `list_registered_tools`
- `reload_registry`

## Requirements

- Docker Desktop or Docker Engine
- Codex or another MCP-capable client
- A host directory to mount as `/data`

On Windows, use PowerShell examples below. On macOS/Linux, replace the host data
path with an absolute POSIX path such as `/home/alice/bio-data:/data`.

## Build

```powershell
docker build --platform=linux/amd64 -t bio-mcp .
```

If Docker Hub or Debian mirrors are unstable, retry the build. The Dockerfile
uses `python:3.11-slim-bookworm` to avoid the package churn of newer Debian
slim tags.

## Run With stdio

Use stdio mode when one MCP client should launch its own container.

```powershell
docker run --rm -i `
  -v <your-data-dir>:/data `
  bio-mcp
```

Example on Windows:

```powershell
docker run --rm -i `
  -v E:\bio-data:/data `
  bio-mcp
```

The `-i` flag is required because MCP stdio transport communicates over stdin
and stdout.

## Run As A Shared HTTP MCP Server

Use HTTP mode when multiple agents should share one running MCP server and the
same in-memory tool registry.

```powershell
docker run -d --name bio-mcp-http --restart unless-stopped `
  -p 127.0.0.1:8765:8000 `
  -v <your-data-dir>:/data `
  -e MCP_TRANSPORT=streamable-http `
  -e MCP_HOST=0.0.0.0 `
  -e MCP_PORT=8000 `
  -e MCP_PATH=/mcp `
  bio-mcp
```

The server will be available at:

```text
http://127.0.0.1:8765/mcp
```

The container binds only to `127.0.0.1` in the example above, so it is not
exposed to your LAN.

Useful commands:

```powershell
docker ps --filter name=bio-mcp-http
docker logs -f bio-mcp-http
docker rm -f bio-mcp-http
```

## Register With Codex

### Recommended: Shared HTTP MCP

Start the HTTP container first, then register the server URL:

```powershell
codex mcp add bio-mcp --url http://127.0.0.1:8765/mcp
```

Equivalent `config.toml` entry:

```toml
[mcp_servers.bio-mcp]
url = "http://127.0.0.1:8765/mcp"
```

Open a new Codex thread, or restart Codex, so the MCP server list is refreshed.

### Alternative: stdio MCP

Use this if you want Codex to launch a fresh container per MCP session:

```toml
[mcp_servers.bio-mcp]
type = "stdio"
command = "docker"
args = ["run", "--rm", "-i", "-v", "<your-data-dir>:/data", "bio-mcp"]
enabled = true
```

For Windows paths in TOML, escape backslashes:

```toml
args = ["run", "--rm", "-i", "-v", "E:\\bio-data:/data", "bio-mcp"]
```

## Data And Persistence

`/data` is the main persistence boundary. Mount it to a host directory.

```text
/data/
  mcp_registry.yaml       # Agent-added persistent tool schemas
  mcp_tools/bin/          # Persistent binaries installed by binary_url
  mcp_outputs/            # Full raw outputs intercepted by the data firewall
  your_input_files...     # FASTA, FASTQ, BAM, BED, PDF, TSV, etc.
```

Built-in tools live in `/app/registry.yaml` inside the image. User-added tools
live in `/data/mcp_registry.yaml`. At startup, the server merges both registries
into one effective MCP tool list.

## Dynamic Tool Installation

### `install_bio_tool`

Installs a bioinformatics tool into the running server environment.

Parameters:

```json
{
  "method": "binary_url",
  "package_name": "seqkit",
  "download_url": "https://github.com/shenwei356/seqkit/releases/download/v2.13.0/seqkit_linux_amd64.tar.gz",
  "binary_name": "seqkit"
}
```

Supported methods:

- `binary_url`: downloads a tar/zip or direct binary, extracts `binary_name`,
  makes it executable, and stores it under `/data/mcp_tools/bin`.
- `apt`: installs an apt package inside the current container. This is useful
  for quick experiments, but it is not persistent across new containers. For
  permanent apt tools, add them to the Dockerfile and rebuild the image.

### `append_tool_to_registry`

Adds a single tool schema to `/data/mcp_registry.yaml` and hot-registers it in
the running server.

Example: register SeqKit stats after installing the `seqkit` binary.

```yaml
name: seqkit_stats
type: cli
command: "seqkit stats {fasta_path}"
description: "Run SeqKit stats on a FASTA/FASTQ file."
output_control:
  intercept_large_output: true
  max_preview_lines: 20
inputs:
  fasta_path:
    type: string
    description: "Path to FASTA/FASTQ file inside /data."
```

Because `/data/mcp_tools/bin` is prepended to `PATH`, commands can refer to
`seqkit` directly instead of using `/data/mcp_tools/bin/seqkit`.

### Registry Refresh Helpers

- `list_registered_tools`: returns the merged registry, tool counts, sources,
  and runtime-registered tool names.
- `reload_registry`: reloads `/app/registry.yaml` and `/data/mcp_registry.yaml`
  into the running server. This is useful after manually editing
  `/data/mcp_registry.yaml`.

`append_tool_to_registry` already reloads automatically. However, some MCP
clients cache the tool list. If Codex does not show a newly added tool in the
current thread, open a new thread or reconnect the MCP server.

## Registry Schema

Each tool entry has this general shape:

```yaml
tools:
  - name: example_tool
    type: cli
    command: "example --input {input_path}"
    description: "Describe what the tool does."
    output_control:
      intercept_large_output: true
      max_preview_lines: 50
    inputs:
      input_path:
        type: string
        description: "Path to input file inside /data."
    expected_outputs:
      - name: output_path
        render_as: dataframe
```

Supported `type` values:

- `cli`
- `java`
- `script`

Supported `render_as` values:

- `image`
- `dataframe`
- `pdf`
- `text`
- `auto`

Path-like inputs are resolved inside `/data`. Relative paths are interpreted
relative to `/data`.

## Data Firewall And Output Handling

For tools with:

```yaml
output_control:
  intercept_large_output: true
  max_preview_lines: 50
```

the server counts stdout/stderr lines. If output exceeds `max_preview_lines`,
the full text is written to:

```text
/data/mcp_outputs/bio_output_<uuid>.txt
```

The MCP response includes:

- preview text
- total line count
- number of preview lines
- local path to the full output

This prevents agents from flooding the context window with multi-megabyte or
multi-gigabyte outputs.

## Security Notes

- Commands are never executed with `shell=True`.
- Command strings are rendered from YAML and split with `shlex.split`.
- Path parameters must resolve inside `/data`.
- Archive extraction blocks unsafe paths that escape the extraction directory.
- HTTP examples bind to `127.0.0.1` on the host.
- `install_bio_tool` is privileged. Only expose this server to trusted local
  agents.

## Troubleshooting

### Docker cannot pull `python:3.11-slim-bookworm`

Retry the build or configure Docker Desktop with a working registry mirror.
Network failures usually happen before the project Dockerfile logic runs.

### apt returns 404 or 502

Debian mirrors can be flaky. Retry the build. The Dockerfile uses apt retry
options, but mirror outages can still fail.

### Codex does not show a newly registered tool

The server hot-registers tools immediately, but Codex may cache the MCP tool
list for the current thread. Use one of these:

- open a new Codex thread
- reconnect/restart the MCP server
- call `list_registered_tools` to confirm the server-side registry updated

### Registered tool fails with "executable not found"

Check that the binary exists in:

```text
/data/mcp_tools/bin
```

For `binary_url`, verify `binary_name` exactly matches the executable inside
the downloaded archive.

### A path is rejected by the data firewall

All tool paths must resolve inside `/data`. Mount your host data directory and
pass paths such as:

```text
/data/sample.bam
```

### apt-installed tools disappear

`method="apt"` installs into the current container only. To make apt tools
permanent, add them to the Dockerfile and rebuild. For downloadable standalone
binaries, prefer `method="binary_url"`.

## Development

Run a syntax check locally:

```powershell
python -m py_compile server.py
```

Build the image:

```powershell
docker build --platform=linux/amd64 -t bio-mcp .
```

Inspect the effective registry from an MCP client with:

```text
list_registered_tools
```

## License

TBD.
