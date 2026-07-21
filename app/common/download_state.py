# coding: utf-8
from enum import Enum


class DownloadState(Enum):
    WAITING  = "waiting"
    RUNNING  = "running"
    PAUSED   = "paused"
    FINISHED = "finished"
    ERROR    = "error"
