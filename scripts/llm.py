"""Ollama helpers shared by scripts/enrich.py and scripts/boost.py.

The Ollama MLX runner rejects structured output (HTTP 501), so there is no `format=`
argument anywhere: the JSON shape is requested in the prompt, parsed and validated here,
and a failed reply gets a repair turn that shows the model what was wrong.
"""
import importlib.util
import re
from pathlib import Path


class LLMError(Exception):
    pass


def load_prompt(version, root, area):
    """Load prompts/<area>/<version>.py as a module. The version must look like v1, v2, ..."""
    path = Path(root) / "prompts" / area / (version + ".py")
    if not re.fullmatch(r"v\d+", version) or not path.is_file():
        raise LLMError("prompt version %r not found (expected %s)" % (version, path))
    spec = importlib.util.spec_from_file_location("%s_prompt_%s" % (area, version), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def models_from_config(config):
    """(enricher models, booster model) from config.json "models". Missing values give ([], None).

    "enricher" is a model name or a list of names (every one is run and stored side by side);
    "booster" is one model name (the stronger model the boost step uses and ranks with).
    """
    section = config.get("models")
    if section is None:
        return [], None
    if not isinstance(section, dict):
        raise LLMError('config.json "models" must be an object like {"enricher": [...], "booster": "..."}')
    enricher = section.get("enricher")
    names = [] if enricher is None else [enricher] if isinstance(enricher, str) else enricher
    if not isinstance(names, list) or any(not isinstance(n, str) or not n.strip() for n in names):
        raise LLMError('config.json models.enricher must be a model name or a list of model names')
    booster = section.get("booster")
    if booster is not None and (not isinstance(booster, str) or not booster.strip()):
        raise LLMError("config.json models.booster must be a model name")
    return list(dict.fromkeys(n.strip() for n in names)), (booster.strip() if booster else None)


def call_model(client, model, messages, num_ctx, think):
    kwargs = dict(model=model, messages=messages,
                  options={"temperature": 0, "num_ctx": num_ctx}, think=think)
    try:
        resp = client.chat(**kwargs)
    except Exception as e:
        # some models reject the think flag; retry once without it
        if "think" in str(e).lower():
            kwargs.pop("think")
            resp = client.chat(**kwargs)
        else:
            raise
    return resp["message"]["content"] if isinstance(resp, dict) else resp.message.content


def extract_json(text):
    """Pull the JSON object out of a reply, tolerating code fences and chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return text[start:end + 1]


def generate_json(client, model, messages, parse, num_ctx, think, retries):
    """Call the model until `parse(reply)` succeeds. Returns (value, error, attempts).

    `parse` gets the raw reply text and raises on anything invalid. After a failed attempt
    the model sees its own reply and the problem, and is asked for a corrected object.
    """
    error = None
    for attempt in range(retries + 1):
        content = None
        try:
            content = call_model(client, model, messages, num_ctx, think)
            return parse(content), None, attempt + 1
        except Exception as e:  # invalid JSON, schema violation, transport error
            error = "%s: %s" % (type(e).__name__, str(e)[:500])
            if content:
                messages = list(messages[:2]) + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "That reply was not valid. Problem: %s\n"
                                                "Return the corrected JSON object only." % error},
                ]
    return None, error, retries + 1


def generate_text(client, model, messages, num_ctx, think):
    return call_model(client, model, messages, num_ctx, think)


def unload_model(client, model):
    """Ask Ollama to drop the model from memory. Failure is not an error."""
    try:
        client.chat(model=model, messages=[], keep_alive=0)
    except Exception:
        pass


def check_models(client, models):
    try:
        listed = client.list()
    except Exception as e:
        raise LLMError("cannot reach the Ollama server (is `ollama serve` running?): %s" % e)
    entries = listed["models"] if isinstance(listed, dict) else listed.models
    names = set()
    for m in entries:
        name = m["model"] if isinstance(m, dict) else m.model
        names.add(name)
        if name.endswith(":latest"):
            names.add(name[: -len(":latest")])
    missing = [m for m in models if m not in names and (m + ":latest") not in names]
    if missing:
        raise LLMError("model(s) not downloaded: %s (have: %s)" % (", ".join(missing), ", ".join(sorted(names))))
