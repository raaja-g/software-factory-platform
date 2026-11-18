from __future__ import annotations

from langgraph.types import Command

from swfactory.graph.deps import Deps
from swfactory.graph.routing import route_after_reviews, transition
from swfactory.graph.state import JobState


def make(deps: Deps):
    async def after_reviews(state: JobState) -> Command:
        target = route_after_reviews(state)
        return Command(goto=target, update=transition(state, "after_reviews", target))

    return after_reviews
