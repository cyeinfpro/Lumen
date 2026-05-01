from aiogram import Router

from . import actions, generation, menu, retry, start, tasks


def build_root_router() -> Router:
    root = Router()
    root.include_router(start.router)
    root.include_router(menu.router)
    root.include_router(generation.router)
    root.include_router(tasks.router)
    root.include_router(retry.router)
    root.include_router(actions.router)
    return root
