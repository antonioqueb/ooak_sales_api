# -*- coding: utf-8 -*-
import json
import logging
from odoo import http, fields
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

class SalesAPIController(http.Controller):

    # -------------------------------------------------------------------------
    # HELPER: Respuesta JSON
    # -------------------------------------------------------------------------
    def _response(self, data=None, status=200, error=None):
        body = {'status': status}
        if data is not None:
            body['data'] = data
        if error:
            body['error'] = error
            
        return Response(
            json.dumps(body, default=str),
            status=status,
            headers=[
                ('Content-Type', 'application/json'),
                ('Access-Control-Allow-Origin', '*'), 
                ('Access-Control-Allow-Methods', 'POST, OPTIONS'),
                ('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization'),
            ]
        )

    # -------------------------------------------------------------------------
    # HELPER: Autenticación (Bearer Token)
    # -------------------------------------------------------------------------
    def _check_auth(self):
        """Valida que el request traiga el Token correcto configurado en Odoo"""
        auth_header = request.httprequest.headers.get('Authorization')
        if not auth_header:
            return False
        
        # Formato esperado: "Bearer MI_TOKEN_SECRETO"
        try:
            token_type, token = auth_header.split()
            if token_type.lower() != 'bearer':
                return False
        except ValueError:
            return False

        # Obtener token guardado en Odoo (System Parameters)
        stored_token = request.env['ir.config_parameter'].sudo().get_param('ooak.api_secret_token')
        
        # Si no hay token configurado en Odoo, bloqueamos por seguridad
        if not stored_token:
            _logger.warning("Sales API: No token configured in ooak.api_secret_token")
            return False

        return token == stored_token

    # -------------------------------------------------------------------------
    # HELPER: Búsqueda Geográfica
    # -------------------------------------------------------------------------
    def _find_country_state(self, country_code, state_name):
        Country = request.env['res.country'].sudo()
        State = request.env['res.country.state'].sudo()
        
        country = False
        state = False

        if country_code:
            country = Country.search([('code', '=', country_code.upper())], limit=1)
        
        if country and state_name:
            # Intentamos buscar el estado por nombre o código dentro del país
            state = State.search([
                ('country_id', '=', country.id),
                '|', ('name', 'ilike', state_name), ('code', 'ilike', state_name)
            ], limit=1)
            
        return country.id if country else False, state.id if state else False

    # -------------------------------------------------------------------------
    # ENDPOINT: CREAR ORDEN
    # -------------------------------------------------------------------------
    @http.route('/api/sales/create_from_stripe', type='http', auth='public', methods=['POST', 'OPTIONS'], csrf=False, cors='*')
    def create_sale_order(self, **post):
        if request.httprequest.method == 'OPTIONS':
            return self._response(status=200)

        # 1. Seguridad
        if not self._check_auth():
            return self._response(status=401, error="Unauthorized: Invalid Token")

        try:
            # 2. Parsear Datos
            try:
                payload = json.loads(request.httprequest.data)
            except:
                return self._response(status=400, error="Invalid JSON")

            customer_data = payload.get('customer', {})
            items_data = payload.get('items', [])
            stripe_ref = payload.get('stripe_session_id')
            
            if not customer_data.get('email') or not items_data:
                return self._response(status=400, error="Missing email or items")

            # 3. Buscar o Crear Cliente (Partner)
            Partner = request.env['res.partner'].sudo()
            email = customer_data.get('email')
            
            partner = Partner.search([('email', '=', email)], limit=1)
            
            # Datos de dirección
            addr = customer_data.get('address', {})
            country_id, state_id = self._find_country_state(addr.get('country'), addr.get('state'))
            
            vals_partner = {
                'name': customer_data.get('name') or email.split('@')[0],
                'email': email,
                'street': addr.get('line1'),
                'street2': addr.get('line2'),
                'city': addr.get('city'),
                'zip': addr.get('postal_code'),
                'country_id': country_id,
                'state_id': state_id,
                'customer_rank': 1, # Marca como cliente
                'type': 'delivery' # Importante para envíos
            }

            if not partner:
                partner = Partner.create(vals_partner)
                _logger.info(f"API Sales: Created new partner {partner.name}")
            else:
                # Opcional: Actualizar dirección si viene una nueva en el pedido
                # partner.write(vals_partner) 
                pass

            # 4. Preparar Líneas de Pedido
            SaleOrder = request.env['sale.order'].sudo()
            Product = request.env['product.product'].sudo()
            
            order_lines = []
            
            for item in items_data:
                # Intentar buscar producto por Referencia Interna (SKU) o Nombre
                # Asumimos que Next.js manda 'sku' o 'name'
                product = False
                sku = item.get('sku')
                
                if sku:
                    product = Product.search([('default_code', '=', sku)], limit=1)
                
                if not product:
                    # Fallback: Buscar por nombre
                    product = Product.search([('name', 'ilike', item.get('product_name'))], limit=1)

                if not product:
                    # Fallback Final: Usar un producto genérico "Venta Web" para no perder la venta
                    # Este producto debe existir o se crea al vuelo (menos recomendado)
                    # Aquí buscamos uno genérico o fallamos controladamente.
                    product = Product.search([('default_code', '=', 'GENERIC_STRIPE')], limit=1)
                    if not product:
                        # Crear producto servicio genérico si no existe
                        product = Product.create({
                            'name': 'Generic Stripe Product',
                            'default_code': 'GENERIC_STRIPE',
                            'type': 'service',
                            'list_price': 0.0
                        })
                
                order_lines.append((0, 0, {
                    'product_id': product.id,
                    'name': item.get('product_name'), # Mantiene el nombre real que vio el cliente
                    'product_uom_qty': item.get('quantity', 1),
                    'price_unit': item.get('price_unit', 0.0),
                }))

            # 5. Crear Orden de Venta
            order = SaleOrder.create({
                'partner_id': partner.id,
                'partner_shipping_id': partner.id, # Dirección de entrega clave para el módulo de stock
                'partner_invoice_id': partner.id,
                'client_order_ref': stripe_ref, # ID de Sesión Stripe
                'origin': 'Web Checkout',
                'order_line': order_lines,
            })

            # 6. Confirmar Orden
            # Esto dispara la creación del Albarán de Entrega (Delivery Order)
            order.action_confirm()

            _logger.info(f"API Sales: Order confirmed {order.name} for {partner.name}")

            return self._response(data={
                'order_id': order.id,
                'order_name': order.name,
                'status': 'confirmed'
            })

        except Exception as e:
            _logger.error(f"API Sales Error: {str(e)}")
            # Odoo hace rollback automático en excepción
            return self._response(status=500, error=str(e))
