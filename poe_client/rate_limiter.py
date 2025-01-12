import asyncio
import logging
from datetime import datetime
from typing import Dict


class PolicyState(object):
    """Stores state information about a policy."""

    current_hits: int
    restriction: int
    last_request: datetime

    def __init__(self, current_hits, restriction) -> None:
        """State of a single policy."""
        self.current_hits = current_hits
        self.restriction = restriction
        self.last_request = datetime.now()

    def reset(self) -> None:
        """Reset to default values."""
        self.restriction = 0
        self.current_hits = 0
        self.last_request = datetime.now()


class Policy(object):
    """Class for tracking an individual rate limit policy."""

    name: str
    max_hits: int
    period: int
    restriction: int
    state: PolicyState

    mutex: asyncio.Lock

    def __init__(self, name: str, max_hits: int, period: int, restriction: int):
        """Initialize a new policy."""
        self.name = name
        self.max_hits = max_hits
        self.period = period
        self.restriction = restriction
        self.state = PolicyState(current_hits=0, restriction=0)
        self.mutex = asyncio.Lock()

    async def update_state(self, current_hits: int, restriction: int):
        """Update the state of the policy."""
        async with self.mutex:
            self.state.current_hits = current_hits
            self.state.restriction = restriction

    async def get_semaphore(self) -> bool:
        """Check state to see if request is allowed."""
        # If last request was restricted, wait and allow
        if self.state.restriction:
            logging.info(
                "Rate limiter restricted. Sleeping for {0} seconds".format(
                    self.state.restriction
                )
            )
            await asyncio.sleep(self.state.restriction)
            return True

        if self.state.current_hits >= self.max_hits:
            logging.info(
                "Rate limiter max hits reached. Sleeping for {0} seconds".format(
                    self.period
                )
            )
            await asyncio.sleep(self.period)
            return True

        # If we haven't reached the quota, increase and allow
        if self.state.current_hits < self.max_hits:
            await self.update_state(self.state.current_hits + 1, self.state.restriction)
            return True

        # Don't allow by default
        return False


class RateLimiter(object):
    """Class for supporting the PoE API rate limitation."""

    policies: Dict[str, Dict[str, Policy]]
    mutex: asyncio.Lock

    def __init__(self):
        """Initialize a new RateLimiter."""
        self.policies = {}
        self.mutex = asyncio.Lock()

    async def parse_headers(self, headers) -> str:
        """Parse response headers into policies.

        Returns the rate limity policy found in the headers, or an empty string if it
        wasn't found.
        """
        if not headers.get("X-Rate-Limit-Policy"):
            return ""

        policy_name = headers["X-Rate-Limit-Policy"]

        rule_names = headers["X-Rate-Limit-Rules"].split(",")
        for rule_name in rule_names:
            policy_id = "{0}/{1}".format(policy_name, rule_name)

            if policy_id not in self.policies.keys():
                async with self.mutex:
                    self.policies[policy_id] = {}

            for rule in headers["X-Rate-Limit-{0}".format(rule_name)].split(","):
                hits, period, restriction = rule.split(":")

                if period not in self.policies[policy_id].keys():
                    async with self.mutex:
                        self.policies[policy_id][period] = Policy(
                            rule,
                            int(hits),
                            int(period),
                            int(restriction),
                        )

            for state in headers["X-Rate-Limit-{0}-State".format(rule_name)].split(
                ",",
            ):
                hits, period, restriction = state.split(":")
                await self.policies[policy_id][period].update_state(
                    int(hits),
                    int(restriction),
                )

        return policy_name

    async def get_semaphore(self, policy_name: str) -> bool:
        """Get a semaphore to make a request."""
        async with self.mutex:
            if not self.policies:
                return False

            semaphores = []
            for name, policy in self.policies.items():
                if not name.startswith(policy_name):
                    continue

                for limit in policy.values():
                    semaphores.append(limit.get_semaphore())

            if semaphores:
                await asyncio.gather(*semaphores)
            return True
