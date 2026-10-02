# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

class ScriptWSManager:

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        # 等待连接
        await ws.accept()
        self.active_connections.append(ws)

    async def disconnect(self, ws: WebSocket):
        # 关闭时 移除ws对象
        if ws in self.active_connections:
            self.active_connections.remove(ws)
        try:
            await ws.close()
        except (RuntimeError, Exception):
            # WebSocket已断开时再close会报 "Unexpected ASGI message"，忽略即可
            pass

    async def broadcast(self, message: str):
        # 广播消息
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except RuntimeError:
                disconnected.append(connection)
        for ws in disconnected:
            await self.disconnect(ws)

    async def broadcast_state(self, data: dict):
        # 广播自身的状态
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(data)
            except RuntimeError:
                disconnected.append(connection)
        for ws in disconnected:
            await self.disconnect(ws)

    async def send_state(self, websocket: WebSocket, data: dict):
        """仅向单个 socket 定向发送状态 JSON，不广播（用于新连接首帧）。"""
        try:
            await websocket.send_json(data)
        except RuntimeError:
            await self.disconnect(websocket)

    async def broadcast_log(self, log: str):
        # 广播日志
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(log)
            except RuntimeError:
                disconnected.append(connection)
        for ws in disconnected:
            await self.disconnect(ws)





