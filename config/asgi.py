# import os
# from django.core.asgi import get_asgi_application
# from channels.routing import ProtocolTypeRouter, URLRouter
# from channels.auth import AuthMiddlewareStack
# import calls.routing  # Points to your calls app routing file

# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings') # 🔥 Set to 'config.settings'

# application = ProtocolTypeRouter({
#     # 🌐 Standard Web Traffic
#     "http": get_asgi_application(),
    
#     # 📱 Real-Time Android Voice Traffic
#     "websocket": AuthMiddlewareStack(
#         URLRouter(
#             calls.routing.websocket_urlpatterns
#         )
#     ),
# })

import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from config.routing import websocket_urlpatterns

# ProtocolTypeRouter directly handling HTTP & WebSockets
application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(
            websocket_urlpatterns
        )
    ),
})