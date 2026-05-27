import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from zerolan.data.pipeline.llm import LLMQuery
from manager.config_manager import get_config
from pipeline.llm.llm_sync import LLMSyncPipeline

config = get_config()
llm = LLMSyncPipeline(config.pipeline.llm)

query = LLMQuery(text="你好，请用一句话介绍你自己。", history=[])
prediction = llm.predict(query)

print("=" * 40)
print("LLM 的回复是：")
print(prediction.response)
print("=" * 40)