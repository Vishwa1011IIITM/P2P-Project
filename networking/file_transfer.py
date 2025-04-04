import asyncio
import os
import uuid
import aiofiles
import hashlib
import logging
import json
from enum import Enum
from networking.shared_state import active_transfers, shutdown_event

class TransferState(Enum):
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

class FileTransfer:
    def __init__(self, file_path, peer_ip, direction="send"):
        self.file_path = file_path
        self.peer_ip = peer_ip
        self.direction = direction
        self.transfer_id = str(uuid.uuid4())
        self.total_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        self.transferred_size = 0
        self.file_handle = None
        self.state = TransferState.IN_PROGRESS
        self.hash_algo = hashlib.sha256()
        self.expected_hash = None
        self.condition = asyncio.Condition()

    async def pause(self):
        """Pause the file transfer."""
        async with self.condition:
            if self.state == TransferState.IN_PROGRESS:
                self.state = TransferState.PAUSED
                logging.info(f"Transfer {self.transfer_id} paused.")

    async def resume(self):
        """Resume the file transfer."""
        async with self.condition:
            if self.state == TransferState.PAUSED:
                self.state = TransferState.IN_PROGRESS
                self.condition.notify_all()
                logging.info(f"Transfer {self.transfer_id} resumed.")

async def compute_hash(file_path):
    """Compute the SHA-256 hash of a file in chunks asynchronously."""
    hash_algo = hashlib.sha256()
    async with aiofiles.open(file_path, "rb") as f:
        while True:
            chunk = await f.read(1024 * 1024)  # Read 1MB chunks
            if not chunk:
                break
            hash_algo.update(chunk)  # Update hash with each chunk
    return hash_algo.hexdigest()

async def send_file(file_path, peers):
    """Send a file to specified peers concurrently."""
    transfer_id = str(uuid.uuid4())
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)

    # Compute hash in chunks
    file_hash = await compute_hash(file_path)

    # Initialize transfer object
    transfer = FileTransfer(file_path, list(peers.keys())[0], direction="send")
    transfer.transfer_id = transfer_id
    transfer.total_size = file_size
    transfer.expected_hash = file_hash
    active_transfers[transfer_id] = transfer

    # Send initialization message
    init_message = json.dumps({
        "type": "file_transfer_init",
        "transfer_id": transfer_id,
        "filename": file_name,
        "filesize": file_size,
        "file_hash": file_hash
    })

    for peer_ip, websocket in peers.items():
        try:
            await websocket.send(init_message)
        except Exception as e:
            logging.error(f"Failed to send file init to {peer_ip}: {e}")
            del active_transfers[transfer_id]
            return

    # Send file chunks
    async with aiofiles.open(file_path, "rb") as f:
        transfer.file_handle = f
        chunk_size = 1024 * 1024  # 1MB chunks
        while not shutdown_event.is_set():
            async with transfer.condition:
                while transfer.state == TransferState.PAUSED and not shutdown_event.is_set():
                    await transfer.condition.wait()
                if shutdown_event.is_set():
                    break
                chunk = await f.read(chunk_size)
                if not chunk:
                    transfer.state = TransferState.COMPLETED
                    break
                transfer.transferred_size += len(chunk)
                for peer_ip, websocket in list(peers.items()):
                    try:
                        await websocket.send(json.dumps({
                            "type": "file_chunk",
                            "transfer_id": transfer_id,
                            "chunk": chunk.hex()
                        }))
                    except Exception as e:
                        logging.error(f"Error sending chunk to {peer_ip}: {e}")
                        del peers[peer_ip]
                if not peers:
                    transfer.state = TransferState.FAILED
                    break
        if transfer.file_handle:
            await transfer.file_handle.close()
            transfer.file_handle = None
    if transfer.state == TransferState.COMPLETED:
        logging.info(f"File transfer {transfer_id} completed.")

async def update_transfer_progress():
    """Update the progress of active file transfers."""
    while not shutdown_event.is_set():
        try:
            for transfer_id, transfer in list(active_transfers.items()):
                if transfer.state in (TransferState.COMPLETED, TransferState.FAILED):
                    if transfer.file_handle:
                        await transfer.file_handle.close()
                    del active_transfers[transfer_id]
                elif transfer.total_size > 0:
                    progress = (transfer.transferred_size / transfer.total_size) * 100
                    logging.debug(f"Transfer {transfer_id}: {progress:.2f}%")
            await asyncio.sleep(1)
        except Exception as e:
            logging.exception(f"Error in update_transfer_progress: {e}")
            await asyncio.sleep(1)