"""OpenAI provider adapters.

Deliberately thin: importing this package must not pull the SDK or a
client into processes that never judge anything. Import the adapter by
path instead:

    from app.integrations.openai.semantic_matcher import OpenAISemanticMatcher
"""
