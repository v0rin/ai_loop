"""OpenRouter-backed Transport -- the real model behind the roles.

Adapted from simple_fusion_call.py. The OpenRouter SDK is imported lazily inside
__init__ so the rest of the package imports fine without it installed; you only
need the SDK (and a key, and network) when you actually construct this.

Cost is recorded as real dollars: guard() before the call (halts if already over
the ceiling), record(cost=...) after with the usage cost. So `ceiling` is in
dollars here, not the abstract units the mock transports use.

supervised=True prints the prompt (input) and the raw reply (output) for every
call and blocks for an Enter keypress before continuing -- step through a run and
watch where a model misformats and the re-prompt fires.
"""
from __future__ import annotations

from typing import Optional

from model_io import Transport, FieldSpec


class OpenRouterTransport(Transport):
    def __init__(self, budget, *, api_key: str,
                 role_models: Optional[dict[str, str]] = None,
                 default_model: str = "deepseek/deepseek-v4-flash",
                 max_cost_per_call: float = 0.05,
                 supervised: bool = False,
                 web_search: bool = True,
                 web_max_results: int = 5,
                 max_tool_steps: int = 4,
                 use_fusion: bool = False,
                 fusion_judge: Optional[str] = None,
                 fusion_panel: Optional[list[str]] = None,
                 fusion_roles: tuple[str, ...] = ("reviewer",)):
        super().__init__(budget)
        # Lazy import: importing this module must not require the SDK.
        from openrouter import OpenRouter
        from openrouter.components.chatusermessage import ChatUserMessage
        from openrouter.components.fusionplugin import FusionPlugin
        from openrouter.components.stopservertoolswhenmaxcost import StopServerToolsWhenMaxCost
        from openrouter.components.stopservertoolswhenstepcountis import (
            StopServerToolsWhenStepCountIs,
        )
        self._UserMsg = ChatUserMessage
        self._Fusion = FusionPlugin
        self._StopCost = StopServerToolsWhenMaxCost
        self._StopStep = StopServerToolsWhenStepCountIs

        self.client = OpenRouter(api_key=api_key)
        self.role_models = role_models or {}
        self.default_model = default_model
        self.max_cost_per_call = max_cost_per_call
        self.supervised = supervised
        self.web_search = web_search
        self.web_max_results = web_max_results
        self.max_tool_steps = max_tool_steps
        self.use_fusion = use_fusion
        self.fusion_judge = fusion_judge or default_model
        self.fusion_panel = fusion_panel or [default_model]
        self.fusion_roles = set(fusion_roles)

    # --- Transport API: override call() so we can record actual cost AFTER ---- #

    def call(self, role: str, prompt: str, schema: list[FieldSpec], hint: str = "") -> str:
        self.budget.guard()                       # halt if already over ceiling
        model = self._model_for(role)
        if self.supervised:
            print(f"\n----- LLM INPUT · role={role} model={model} hint={hint} -----")
            print(prompt)
        raw, cost, t_in, t_out = self._send(role, prompt)
        if self.supervised:
            print(f"\n----- LLM OUTPUT · role={role}  (${cost:.4f}, {t_in} in / {t_out} out) -----")
            print(raw)
            input("  [supervised] Enter to continue (Ctrl-C aborts) ... ")
        self.budget.record(role, hint, cost=cost)
        return raw

    def _produce(self, role, prompt, schema):     # unused; call() is overridden
        raise NotImplementedError

    # --- helpers -------------------------------------------------------------- #

    def _model_for(self, role: str) -> str:
        return self.role_models.get(role, self.default_model)

    @staticmethod
    def _message_text(message) -> str:
        content = message.content
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        parts = []
        for part in content:
            text = getattr(part, "text", None)
            if text is not None:
                parts.append(text)
        return "".join(parts)

    def _send(self, role: str, prompt: str):
        model = self._model_for(role)
        plugins = []
        if self.web_search:
            # plugins accept plain dicts (ChatRequestPluginTypedDict, confirmed in SDK)
            plugins.append({"id": "web", "max_results": self.web_max_results})
        if self.use_fusion and role in self.fusion_roles:
            plugins.append(self._Fusion(id="fusion", model=self.fusion_judge,
                                        analysis_models=self.fusion_panel,
                                        preset="general-budget"))
            model = self.fusion_judge

        result = self.client.chat.send(
            model=model,
            plugins=plugins,
            messages=[self._UserMsg(role="user", content=prompt)],
            stop_server_tools_when=[
                self._StopStep(step_count=self.max_tool_steps, type="step_count_is"),
                self._StopCost(max_cost_in_dollars=self.max_cost_per_call, type="max_cost"),
            ],
        )
        message = result.choices[0].message
        raw = self._message_text(message)
        usage = result.usage
        t_in = getattr(usage, "prompt_tokens", None) if usage else None
        t_out = getattr(usage, "completion_tokens", None) if usage else None
        cost = getattr(usage, "cost", None) if usage else None
        if cost is None:
            # Keep the dollar budget meaningful even if usage.cost is absent.
            cost = self.max_cost_per_call
        return raw, float(cost), t_in or 0, t_out or 0
