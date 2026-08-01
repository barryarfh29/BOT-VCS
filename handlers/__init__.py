"""
Handlers package - Admin, Customer, and Input handlers
"""

from handlers.admin import register_admin_handlers
from handlers.customer import register_customer_handlers, register_bukti_handlers
from handlers.input import register_input_handlers


def register_all_handlers():
    """Register all handler groups.
    Order matters: input handlers (video/photo/text) first so they match media messages
    before bukti_handlers photo catches them.
    """
    register_admin_handlers()
    register_customer_handlers()
    register_input_handlers()   # video+photo admin handlers — before bukti
    register_bukti_handlers()   # bukti photo handler — after input
