"""Pluggable AI provider package (Anthropic, Bedrock, OpenAI, Ollama, Gemini).

The registry + adapter contract live in ``base.py``; the family adapters in
``anthropic_provider.py`` / ``openai_provider.py`` self-register at import;
``service.py`` resolves the single active provider from config. The LLM pipeline
(``app/services/llm_service.py``) calls ``service.active_provider()`` and stays
provider-agnostic.
"""
