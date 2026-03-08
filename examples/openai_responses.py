from __future__ import annotations

import os

from openai import OpenAI


def main() -> None:
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY", "ob-<your-key-here>"),
        base_url=os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8899/v1"),
    )

    response = client.responses.create(
        model="gpt-5.4",
        input="Write a one-sentence bedtime story about a unicorn.",
    )

    print("id:", response.id)
    print("text:", response.output_text)


if __name__ == "__main__":
    main()
