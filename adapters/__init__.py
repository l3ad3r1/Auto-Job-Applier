from .indeed import IndeedAdapter
from .linkedin import LinkedInAdapter
from .naukri import NaukriAdapter

ADAPTERS = {
    "linkedin": LinkedInAdapter,
    "naukri": NaukriAdapter,
    "indeed": IndeedAdapter,
}


def get_adapter(platform: str):
    try:
        return ADAPTERS[platform]()
    except KeyError:
        raise SystemExit(
            f"Unknown platform '{platform}'. Available: {', '.join(ADAPTERS)}")
