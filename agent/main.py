"""AgentCore Runtime code-zip entry point, copied to the zip root.

The Runtime EntryPoint is ``["opentelemetry-instrument", "main.py"]``.
"""

from fast_channels.agent.main import serve

if __name__ == "__main__":
    serve()
