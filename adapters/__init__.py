from .foundit import FounditAdapter
from .indeed import IndeedAdapter
from .linkedin import LinkedInAdapter
from .naukri import NaukriAdapter
from .shine import ShineAdapter

ADAPTERS = {
    "linkedin": LinkedInAdapter,
    "naukri": NaukriAdapter,
    "indeed": IndeedAdapter,
    "foundit": FounditAdapter,
    "shine": ShineAdapter,
}


def get_adapter(platform: str):
    try:
        return ADAPTERS[platform]()
    except KeyError:
        raise SystemExit(
            f"Unknown platform '{platform}'. Available: {', '.join(ADAPTERS)}")
