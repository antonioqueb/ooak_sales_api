{
    'name': 'OOAK Sales & Stripe API',
    'version': '1.0.0',
    'category': 'Sales/API',
    'summary': 'API Segura para recibir órdenes desde Stripe/Next.js',
    'description': """
        Módulo Backend para procesar Webhooks de Ventas.
        
        Funcionalidades:
        - Endpoint seguro para creación de Sale Orders.
        - Búsqueda inteligente de Clientes (por email).
        - Gestión de Direcciones de Envío (País/Estado).
        - Confirmación automática de pedidos (Reserva de Stock).
        - Registro de referencia de pago Stripe.
    """,
    'author': 'AlphaQueb',
    'depends': ['base', 'sale_management', 'stock', 'contacts'],
    'data': [
        'security/ir.model.access.csv',
        'data/system_params.xml',
    ],
    'application': True,
    'installable': True,
    'license': 'LGPL-3',
}
