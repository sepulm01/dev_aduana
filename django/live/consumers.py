import json

from channels.generic.websocket import AsyncWebsocketConsumer


class DeviceConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.device_id = self.scope["url_route"]["kwargs"]["device_id"]
        self.room_group_name = f"device_{self.device_id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name,
        )

    async def receive(self, text_data):
        data = json.loads(text_data)
        msg_type = data.get("type", "")

        if msg_type == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    async def motion_event(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "motion",
                    "device_id": event["device_id"],
                    "timestamp": event["timestamp"],
                    "metadata": event.get("metadata", {}),
                }
            )
        )

    async def device_status(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "status",
                    "device_id": event["device_id"],
                    "online": event.get("online", False),
                }
            )
        )
