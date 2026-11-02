def main() -> None:
    """Dev entry point (`uv run shortly`). Production runs uvicorn directly in the container."""
    import uvicorn

    uvicorn.run(
        "shortly.api:create_app_from_env",
        factory=True,
        host="127.0.0.1",
        port=8000,
        access_log=False,
    )
