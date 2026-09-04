from engine_backend.agent_config import merchant_agent_config, shopping_agent_config


def test_deployment_configs_carry_the_live_eval_remediations():
    shopping = shopping_agent_config()
    merchant = merchant_agent_config()

    assert shopping.brand_name == merchant.brand_name == "ACME Supply"
    assert "qualified clinician" in shopping.brand_voice
    assert "manufacturer documentation is not a substitute" in shopping.brand_voice
    assert "never call it applied" in merchant.brand_voice
    assert "unless apply_change itself succeeded" in merchant.brand_voice
