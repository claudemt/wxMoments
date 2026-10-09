class WxMomentsError(Exception):
    pass

class KeyAcquisitionError(WxMomentsError):
    pass

class DatabaseError(WxMomentsError):
    pass

class MediaError(WxMomentsError):
    pass

class NetworkError(WxMomentsError):
    pass
