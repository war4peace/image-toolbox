"""
First-start Wizard (0.4.6): the Ollama "is the recommended model already
installed?" matcher that decides whether the pull step offers a download.

The nuance pinned here is Ollama's ':latest' normalisation: `ollama pull minicpm-v`
is stored and listed as `minicpm-v:latest`, so a bare wanted name and its ':latest'
form must be treated as equal, while a different explicit tag (qwen2.5vl:3b vs
qwen2.5vl:7b) must NOT satisfy the wanted one. Pure, stdlib-only, no server.
"""
import gui.common as common


def test_exact_tag_matches():
    installed = ["qwen2.5vl:7b", "gemma3:4b"]
    assert common.ollama_model_present(installed, "qwen2.5vl:7b")
    assert common.ollama_model_present(installed, "gemma3:4b")


def test_latest_normalisation_both_directions():
    # Installed bare, wanted ':latest' (the minicpm-v case: pulled without a tag).
    assert common.ollama_model_present(["minicpm-v:latest"], "minicpm-v:latest")
    assert common.ollama_model_present(["minicpm-v"], "minicpm-v:latest")
    assert common.ollama_model_present(["minicpm-v:latest"], "minicpm-v")


def test_different_explicit_tag_does_not_match():
    # A 3B variant must not count as the recommended 7B one.
    assert not common.ollama_model_present(["qwen2.5vl:3b"], "qwen2.5vl:7b")
    assert not common.ollama_model_present(["gemma3:12b"], "gemma3:4b")


def test_absent_model_is_not_present():
    assert not common.ollama_model_present([], "gemma3:4b")
    assert not common.ollama_model_present(["llava:7b"], "minicpm-v:latest")


def test_tag_matches_helper_directly():
    assert common._ollama_tag_matches("minicpm-v", "minicpm-v:latest")
    assert common._ollama_tag_matches("gemma3:4b", "gemma3:4b")
    assert not common._ollama_tag_matches("gemma3:12b", "gemma3:4b")
