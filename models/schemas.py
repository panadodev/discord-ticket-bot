from pydantic import BaseModel


class Record(BaseModel):
    field1: str
    description2: int


class ChannelRequest(BaseModel):
    name: str
    message: str
