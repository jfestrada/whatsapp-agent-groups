import os

from mcp.server.fastmcp import FastMCP

from transcribe import transcribe_file

mcp = FastMCP(
    "whisper",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
)


@mcp.tool()
def transcribe(path: str) -> str:
    """Transcribe a local audio file and return the text.

    Args:
        path: Absolute path of an audio file inside the media directory.
    """
    return transcribe_file(path)


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
