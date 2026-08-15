import pytest


class SchemaFakeProvider:
    """Returns pre-built dicts validated against the requested schema."""

    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, system_prompt, user_prompt, schema):
        value = self.responses.pop(0)
        return schema.model_validate(value)


@pytest.fixture
def fake_provider_factory():
    return SchemaFakeProvider
