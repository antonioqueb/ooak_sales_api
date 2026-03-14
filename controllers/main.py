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
        auth_header = request.httprequest.headers.get('Authorization')
        if not auth_header:
            return False
        try:
            token_type, token = auth_header.split()
            if token_type.lower() != 'bearer':
                return False
        except ValueError:
            return False

        stored_token = request.env['ir.config_parameter'].sudo().get_param('ooak.api_secret_token')
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
            state = State.search([
                ('country_id', '=', country.id),
                '|', ('name', 'ilike', state_name), ('code', 'ilike', state_name)
            ], limit=1)

        return country.id if country else False, state.id if state else False

    # -------------------------------------------------------------------------
    # HELPER: Obtener impuesto IVA 16% con precio incluido
    # -------------------------------------------------------------------------
    def _get_tax_included(self, company):
        """
        Busca el IVA 16% de ventas existente en la compañía.
        Si ya tiene price_include=True, lo usa directo.
        Si no, busca el IVA 16% normal y le calcula el precio
        manualmente en la línea (no modifica el impuesto existente).
        
        NO crea impuestos nuevos. Usa el que ya existe.
        """
        Tax = request.env['account.tax'].sudo()

        # 1. Buscar IVA 16% con price_include ya activado
        tax = Tax.search([
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 16.0),
            ('price_include', '=', True),
            ('company_id', '=', company.id),
        ], limit=1)

        if tax:
            return tax

        # 2. No hay uno con price_include, buscar el IVA 16% normal
        tax = Tax.search([
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 16.0),
            ('company_id', '=', company.id),
        ], limit=1)

        if tax:
            # Retornamos el impuesto normal. El precio se ajustará
            # en la línea de pedido dividiendo entre 1.16
            return tax

        # 3. No hay ningún IVA 16% — esto no debería pasar en México
        _logger.warning("API Sales: No IVA 16% tax found for company %s", company.name)
        return False

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
            except Exception:
                return self._response(status=400, error="Invalid JSON")

            customer_data = payload.get('customer', {})
            items_data = payload.get('items', [])
            stripe_ref = payload.get('stripe_session_id')
            shipping_data = payload.get('shipping', {})

            email = customer_data.get('email')
            name = customer_data.get('name')

            if not email or not name or not items_data:
                return self._response(status=400, error="Missing required fields: name, email, items")

            # 3. Buscar o Crear Cliente (Partner) - CONTACTO PRINCIPAL
            Partner = request.env['res.partner'].sudo()

            partner = Partner.search([('email', '=', email)], limit=1)

            billing_addr = customer_data.get('address', {})
            billing_country_id, billing_state_id = self._find_country_state(
                billing_addr.get('country'),
                billing_addr.get('state')
            )

            vals_partner = {
                'name': name,
                'email': email,
                'phone': customer_data.get('phone') or False,
                'street': billing_addr.get('line1') or False,
                'street2': billing_addr.get('line2') or False,
                'city': billing_addr.get('city') or False,
                'zip': billing_addr.get('postal_code') or False,
                'country_id': billing_country_id,
                'state_id': billing_state_id,
                'customer_rank': 1,
            }

            if not partner:
                partner = Partner.create(vals_partner)
                _logger.info(f"API Sales: Created new partner '{partner.name}' (id={partner.id})")
            else:
                partner.write(vals_partner)
                _logger.info(f"API Sales: Updated partner '{partner.name}' (id={partner.id})")

            # 4. Crear/Buscar dirección de ENVÍO como contacto hijo
            ship_addr = shipping_data.get('address', {})
            ship_name = shipping_data.get('name') or name

            ship_country_id, ship_state_id = self._find_country_state(
                ship_addr.get('country'),
                ship_addr.get('state')
            )

            shipping_partner = False
            if ship_addr.get('line1'):
                shipping_partner = Partner.search([
                    ('parent_id', '=', partner.id),
                    ('type', '=', 'delivery'),
                    ('street', '=', ship_addr.get('line1')),
                    ('city', '=', ship_addr.get('city') or False),
                    ('zip', '=', ship_addr.get('postal_code') or False),
                ], limit=1)

                if not shipping_partner:
                    shipping_partner = Partner.create({
                        'parent_id': partner.id,
                        'type': 'delivery',
                        'name': ship_name,
                        'street': ship_addr.get('line1') or False,
                        'street2': ship_addr.get('line2') or False,
                        'city': ship_addr.get('city') or False,
                        'zip': ship_addr.get('postal_code') or False,
                        'country_id': ship_country_id,
                        'state_id': ship_state_id,
                        'phone': customer_data.get('phone') or False,
                    })
                    _logger.info(f"API Sales: Created shipping address for '{partner.name}' -> '{shipping_partner.name}'")
                else:
                    shipping_partner.write({
                        'name': ship_name,
                        'street': ship_addr.get('line1') or False,
                        'street2': ship_addr.get('line2') or False,
                        'city': ship_addr.get('city') or False,
                        'zip': ship_addr.get('postal_code') or False,
                        'country_id': ship_country_id,
                        'state_id': ship_state_id,
                    })

            if not shipping_partner:
                shipping_partner = partner

            # 5. Preparar Líneas de Pedido
            SaleOrder = request.env['sale.order'].sudo()
            Product = request.env['product.product'].sudo()

            company = request.env.company
            tax = self._get_tax_included(company)

            order_lines = []

            for item in items_data:
                product = False
                sku = item.get('sku')

                if sku:
                    product = Product.search([('default_code', '=', sku)], limit=1)

                if not product:
                    product = Product.search([('name', 'ilike', item.get('product_name'))], limit=1)

                if not product:
                    product = Product.search([('default_code', '=', 'GENERIC_STRIPE')], limit=1)
                    if not product:
                        product = Product.create({
                            'name': 'Generic Stripe Product',
                            'default_code': 'GENERIC_STRIPE',
                            'type': 'service',
                            'list_price': 0.0
                        })

                # El precio de Stripe YA incluye IVA.
                # Si el impuesto tiene price_include=True, Odoo lo desglosa solo.
                # Si el impuesto NO tiene price_include, debemos dividir entre 1.16
                # para que Odoo sume el 16% y el total coincida.
                price_from_stripe = item.get('price_unit', 0.0)

                if tax and not tax.price_include:
                    # Impuesto normal (no incluido): extraer base
                    price_unit = round(price_from_stripe / 1.16, 2)
                else:
                    # Impuesto con precio incluido o sin impuesto: usar tal cual
                    price_unit = price_from_stripe

                line_vals = {
                    'product_id': product.id,
                    'name': item.get('product_name') or product.name,
                    'product_uom_qty': item.get('quantity', 1),
                    'price_unit': price_unit,
                }

                if tax:
                    line_vals['tax_ids'] = [(6, 0, [tax.id])]

                order_lines.append((0, 0, line_vals))

            # 6. Crear Orden de Venta
            order = SaleOrder.create({
                'partner_id': partner.id,
                'partner_shipping_id': shipping_partner.id,
                'partner_invoice_id': partner.id,
                'client_order_ref': stripe_ref,
                'origin': 'Web Checkout (Stripe)',
                'order_line': order_lines,
            })

            # 7. Confirmar Orden
            order.action_confirm()

            _logger.info(f"API Sales: Order confirmed {order.name} for {partner.name} | "
                         f"Ship to: {shipping_partner.name} ({shipping_partner.city})")

            return self._response(data={
                'order_id': order.id,
                'order_name': order.name,
                'partner_name': partner.name,
                'shipping_address': f"{shipping_partner.street or ''}, {shipping_partner.city or ''}, {shipping_partner.zip or ''}",
                'status': 'confirmed'
            })

        except Exception as e:
            _logger.error(f"API Sales Error: {str(e)}", exc_info=True)
            return self._response(status=500, error=str(e))