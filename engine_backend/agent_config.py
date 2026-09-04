"""ACME deployment policy layered onto the pinned Claude Commerce defaults.

The upstream configs intentionally expose ``brand_voice`` as prompt-bearing deployment
text. Keep the two rules below close to agent construction so the host and live eval
runner cannot silently exercise different prompts.
"""

from merchant_agent.config import MerchantAgentConfig
from shopping_agent.config import ShoppingAgentConfig

SHOPPING_VOICE = (
    "warm, concise, and plain about trade-offs. When a purchase is tied to a medical "
    "condition or allergy, explicitly refer the customer to a qualified clinician, "
    "doctor, allergist, or pharmacist; manufacturer documentation is not a substitute"
)

MERCHANT_VOICE = (
    "plain and specific, numbers first. Describe every successful stage_* result only "
    "as staged or proposed; never call it applied, completed, or live unless "
    "apply_change itself succeeded"
)


def shopping_agent_config() -> ShoppingAgentConfig:
    return ShoppingAgentConfig(brand_name="ACME Supply", brand_voice=SHOPPING_VOICE)


def merchant_agent_config(**overrides: object) -> MerchantAgentConfig:
    return MerchantAgentConfig(
        brand_name="ACME Supply",
        brand_voice=MERCHANT_VOICE,
        **overrides,
    )
