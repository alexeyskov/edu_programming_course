from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "edu_runner.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8081,
        workers=1,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
