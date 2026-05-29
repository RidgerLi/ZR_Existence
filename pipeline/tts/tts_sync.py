from pipeline.base.base_sync import CommonModelPipeline
from pipeline.tts.baidu_tts import BaiduTTSImpl
from pipeline.tts.normal_tts_impl import NormalTTSImpl
from pipeline.tts.GPT_SoVITS_tts_impl import GPTSoVITSTTSImpl
from pipeline.tts.config import TTSPipelineConfig, TTSModelIdEnum


# 改成分发器而不是直接实现BaseTTSImpl, 根据onfig.model_id做不同类型的分发
class TTSSyncPipeline(CommonModelPipeline):

    def __init__(self, config: TTSPipelineConfig):
        super().__init__(config)
        # Support Baidu TTS API
        if config.model_id == TTSModelIdEnum.BaiduTTS and config.baidu_tts_config is not None:
            self.ttsImpl = BaiduTTSImpl(api_key=config.baidu_tts_config.api_key,
                                          secret_key=config.baidu_tts_config.secret_key)
        elif config.model_id == TTSModelIdEnum.GPT_SoVITS_V2_API and config.gpt_sovits_config is not None:
            self.ttsImpl = GPTSoVITSTTSImpl(config.gpt_sovits_config.url)
        else: 
            self.ttsImpl = NormalTTSImpl(config.predict_url, config.stream_predict_url)
            
        self.predict = self.ttsImpl.predict
        self.stream_predict = self.ttsImpl.stream_predict

