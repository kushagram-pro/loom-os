# processor.py — updated to use attention filter

import asyncio
from database import save_event
from attention_filter import AttentionFilter


async def process_events(queue: asyncio.Queue):
    """
    Pulls events from the queue, runs them through
    the attention filter, and saves the ones that matter.
    """
    print("[Processor] Ready — attention filter active...")

    attention = AttentionFilter()

    while True:
        event = await queue.get()

        try:
            should_store, importance = attention.should_store(event)

            if should_store:
                enriched = attention.enrich(event, importance)
                await save_event(enriched)
                
                # Visual indicator of importance in terminal
                marker = "★" if importance == "high" else "·"
                print(f"[{marker}] {event['source']} | "
                      f"{event['app']} | {event['title'][:50]}")
            
        except Exception as e:
            print(f"[Processor] Error: {e}")

        finally:
            queue.task_done()