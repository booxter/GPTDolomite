# Third Party
from transformers import (
    AutoConfig,
    AutoModel,
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
)

# Local
from .models import GPTDolomiteConfig, GPTDolomiteForCausalLM, GPTDolomiteModel

# (AutoConfig, AutoModel, AutoModelForCausalLM)
_CUSTOM_MODEL_REGISTRY = [
    (GPTDolomiteConfig, GPTDolomiteModel, GPTDolomiteForCausalLM),
]
_CUSTOM_MODEL_TYPES = []
_CUSTOM_MODEL_CLASSES = []


def register_model_classes() -> None:
    for (
        config_class,
        auto_model_class,
        auto_model_for_causal_lm_class,
    ) in _CUSTOM_MODEL_REGISTRY:
        model_type = config_class.model_type

        AutoConfig.register(model_type, config_class)
        AutoModel.register(config_class, auto_model_class)
        AutoModelForCausalLM.register(config_class, auto_model_for_causal_lm_class)

        _CUSTOM_MODEL_TYPES.append(model_type)
        _CUSTOM_MODEL_CLASSES.append(auto_model_for_causal_lm_class)


def is_custom_model(
    model_class: type[AutoModelForCausalLM] | type[AutoModelForSeq2SeqLM],
    model_type: str,
) -> bool:
    return (
        model_class.__name__ in _CUSTOM_MODEL_CLASSES
        or model_type in _CUSTOM_MODEL_TYPES
    )
