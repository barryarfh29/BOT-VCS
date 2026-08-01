"""
Handlers package - Admin, Customer, and Input handlers
"""

from handlers.admin import register_admin_handlers
from handlers.customer import register_customer_handlers, register_bukti_handlers
from handlers.input import register_input_handlers


def register_all_handlers():
    """Register all handler groups."""
    register_admin_handlers()
    register_customer_handlers()
    register_bukti_handlers()
    register_input_handlers()
