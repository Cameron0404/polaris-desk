"""An example custom Source: plug your own agent system into Polaris.

    POLARIS_SOURCES="examples.my_source:MySource" bin/polaris

Run it from the repo root so `examples` imports. Everything here is invented example data.
"""
from polaris.sources import FileSource


class MySource(FileSource):
    name = "my-source"

    def status(self) -> dict:
        doc = super().status()                       # keep the files under feeds/ as a base
        doc["headline"] = "Example: headline from MySource"
        return doc

    def questions(self) -> list:
        return [{"id": "deploy", "question": "Example: deploy the staging build now?",
                 "options": ["yes", "not today"], "from": "example deploy agent"}]

    def answer(self, qid: str, text: str) -> str:
        # Hand the answer back to your own system here: a file, an HTTP call, a queue.
        return f"MySource received {text!r} for {qid}"
