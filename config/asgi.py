# import os
# import django

# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
# django.setup()

# from django.core.asgi import get_asgi_application
# from channels.routing import ProtocolTypeRouter, URLRouter
# from channels.auth import AuthMiddlewareStack
# from config.routing import websocket_urlpatterns

# # ProtocolTypeRouter directly handling HTTP & WebSockets
# application = ProtocolTypeRouter({
#     "http": get_asgi_application(),
#     "websocket": AuthMiddlewareStack(
#         URLRouter(
#             websocket_urlpatterns
#         )
#     ),
# })
# config/asgi.py
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from django.urls import path
from calls.consumers import MediaStreamConsumer

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": URLRouter([
        path("ws/media-stream/", MediaStreamConsumer.as_asgi()), # 👈 Your mobile endpoint
        path("", MediaStreamConsumer.as_asgi()),                  # 👈 Graceful fallback for root requests
    ]),
})