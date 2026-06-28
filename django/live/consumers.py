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

    async def dispatch(self, message):
        msg_type = message.get("type", "")
        if msg_type.startswith("websocket."):
            await super().dispatch(message)
        else:
            await self.send(text_data=json.dumps(message))


class IncidentConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_group_name = "incidents"

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
        if data.get("type") == "ping":
            await self.send(text_data=json.dumps({"type": "pong"}))

    async def incident_alert(self, event):
        await self.send(text_data=json.dumps({
            "type": "incident_alert",
            "incident_id": event["incident_id"],
            "device_id": event["device_id"],
            "device_name": event.get("device_name", ""),
            "incident_type": event.get("incident_type", ""),
            "level": event.get("level", 1),
        }))

    async def incident_status(self, event):
        await self.send(text_data=json.dumps({
            "type": "incident_status",
            "incident_id": event["incident_id"],
            "device_id": event["device_id"],
            "status": event["status"],
        }))
