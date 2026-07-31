from django.urls import re_path
from calls.consumers import MediaStreamConsumer 

websocket_urlpatterns = [
    # 🟢 Flexible trailing slash matcher
    re_path(r'^ws/media-stream/?$', MediaStreamConsumer.as_asgi()),
]