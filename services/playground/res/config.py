from pydantic import BaseModel, Field


class ResourceServerConfig(BaseModel):
    host: str = Field("0.0.0.0", description="The host address of the resource server")
    port: int = Field(8899, description="The port number of the resource server")
    allow_remote_control: bool = Field(False,
                                       description="Allow /control/* (mode switch, microphone, todo list, send message) "
                                                   "from non-loopback clients. Off by default: the resource server binds "
                                                   "0.0.0.0, so leaving it off means only this machine can control the bot.")