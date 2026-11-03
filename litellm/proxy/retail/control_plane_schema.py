from typing import Annotated, TypeAlias

import psycopg
from fastapi import Depends, HTTPException
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from .api import Store
from .store import WalletStore


async def control_plane_store(store: Store) -> WalletStore:
    try:
        await store.initialize_control_plane()
    except (psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests) as exc:
        raise HTTPException(503, "Management storage is temporarily unavailable") from exc
    return store


ControlPlaneStore: TypeAlias = Annotated[WalletStore, Depends(control_plane_store)]
