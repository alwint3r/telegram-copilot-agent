from copilot.generated.session_events import SessionEventType
import os
from copilot import CopilotClient
import asyncio

client = CopilotClient(
    {
        "log_level": "debug",
    }
)


async def main():
    try:
        await client.start()
        session = await client.create_session(
            {
                # "model": "gpt-5-mini",
                # "reasoning_effort": "high",
                "model": "gpt-4.1",
                "working_directory": os.getcwd(),
                "streaming": True,
            }
        )

        while True:
            try:
                user_prompt = input("\nEnter your prompt (or 'exit' to quit): ").strip()
                if user_prompt.lower() == "exit":
                    break
                if not user_prompt:
                    print("Prompt cannot be empty. Please try again.")
                    continue

                done = asyncio.Event()

                def handler(event):
                    if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
                        print(event.data.delta_content, end="", flush=True)
                    elif event.type == SessionEventType.SESSION_IDLE:
                        done.set()

                unsubscribe = session.on(handler)
                await session.send(
                    {"prompt": user_prompt},
                )

                await done.wait()
                unsubscribe()
            except Exception as e:
                print(f"Loop Error: {e}")
        await session.destroy()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
