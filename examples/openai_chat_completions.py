from __future__ import annotations

import os

from openai import OpenAI


def main() -> None:
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY", "ob-<your-key-here>"),
        base_url=os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8899/v1"),
    )

    response = client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant."
            },
            {
                "role": "user",
                "content": "Write a one-sentence bedtime story about a unicorn.",
            },
        ],
    )

    print("id:", response.id)
    print("text:", response.choices[0].message.content)


if __name__ == "__main__":
    main()
