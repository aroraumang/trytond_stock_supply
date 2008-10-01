#This file is part of Tryton.  The COPYRIGHT file at the top level of this repository contains the full copyright notices and license terms.
from trytond.osv import fields, OSV
from trytond.wizard import Wizard, WizardOSV
import datetime

# TODO ensure that the link p_request p_line is never inconsistent
# (uom, qty, product, ...).
class PurchaseRequest(OSV):
    'Purchase Request'
    _name = 'stock.purchase_request'
    _description = __doc__

    product = fields.Many2One(
        'product.product', 'Product', required=True, select=True, readonly=True)
    party = fields.Many2One('relationship.party', 'Party',  select=True)
    quantity = fields.Float('Quantity', required=True)
    uom = fields.Many2One('product.uom', 'UOM', required=True, select=True)
    purchase_date = fields.Date('Best Purchase Date', readonly=True)
    supply_date = fields.Date('Expected Supply Date', readonly=True)
    stock_level =  fields.Float('Stock at Supply Date', readonly=True)
    warehouse = fields.Many2One(
        'stock.location', "Warehouse", required=True,
        domain="[('type', '=', 'warehouse')]", readonly=True)
    purchase_line = fields.Many2One(
        'purchase.line', 'Purchase Line',readonly=True)
    company = fields.Many2One(
        'company.company', 'Company', required=True, readonly=True)

    def default_company(self, cursor, user, context=None):
        company_obj = self.pool.get('company.company')
        if context is None:
            context = {}
        if context.get('company'):
            return company_obj.name_get(cursor, user, context['company'],
                    context=context)[0]
        return False


    def generate_requests(self, cursor, user, context=None):
        """
        For each product compute the purchase request that must be
        create today to meet product outputs.
        """
        order_point_obj = self.pool.get('stock.order_point')
        purchase_request_obj = self.pool.get('stock.purchase_request')
        product_obj = self.pool.get('product.product')
        location_obj = self.pool.get('stock.location')
        user_obj = self.pool.get('res.user')
        company = user_obj.browse(cursor, user, user, context=context).company

        # fetch warehouses:
        warehouse_ids = location_obj.search(
            cursor, user, [('type','=','warehouse')], context=context)
        # fetch order points
        order_point_ids = order_point_obj.search(
            cursor, user, [], context=context)
        # index them by product
        product2ops = {}
        for order_point in order_point_obj.browse(
            cursor, user, order_point_ids, context=context):
            product2ops[
                (order_point.location.id, order_point.product.id)
                ] = order_point

        # fetch stockable products
        product_ids = product_obj.search(
            cursor, user, [('type', '=', 'stockable')], context=context)
        #aggregate product by minimum supply date
        date2products = {}
        for product in product_obj.browse(cursor, user, product_ids,
                                          context=context):
            min_date, max_date = self.get_supply_dates(
                cursor, user, product, context=context)
            date2products.setdefault(min_date, []).append((product, max_date))

        # compute requests
        local_context = context and context.copy() or {}
        new_requests = []
        for min_date in date2products:
            product_ids = [x[0].id for x in date2products[min_date]]
            local_context.update(
                {'stock_date_end': min_date or datetime.date.max})
            pbl = product_obj.products_by_location(
                cursor, user, warehouse_ids, product_ids, with_childs=True,
                skip_zero=False, context=local_context)
            for product, max_date in date2products[min_date]:
                for warehouse_id in warehouse_ids:
                    qty = pbl.pop((warehouse_id, product.id))
                    order_point = product2ops.get((warehouse_id, product.id))
                    # Search for shortage between min-max
                    shortage_date, product_quantity = self.get_shortage(
                        cursor, user, warehouse_id, product.id, min_date,
                        max_date, min_date_qty=qty, order_point=order_point,
                        context=context)

                    if shortage_date == None or product_quantity == None:
                        continue
                    # generate request values
                    request_val = self.compute_request(
                        cursor, user, product, warehouse_id, shortage_date,
                        product_quantity, company, order_point, context=context)
                    new_requests.append(request_val)

        self.create_requests(cursor, user, new_requests, context=context)
        return {}

    def create_requests(self, cursor, user, new_requests, context=None):
        """
        Compare new_requests with already existing request and avoid
        to re-create existing requests.
        """
        # delete purchase request without a purchase line
        uom_obj = self.pool.get('product.uom')
        request_obj = self.pool.get('stock.purchase_request')
        product_supplier_obj = self.pool.get('purchase.product_supplier')
        req_ids = request_obj.search(
            cursor, user, ['OR',[('purchase_line', '=', False)],
                           [('purchase_line.purchase.state', '=', 'cancel')]],
            context=context)
        request_obj.delete(cursor, user, req_ids, context=context)

        req_ids = request_obj.search(cursor, user, [], context=context)
        requests = request_obj.browse(cursor, user, req_ids, context=context)
        # Fetch delivery_times for each (product,supplier)
        sup_delivery_time = {}
        for request in requests:
            product, supplier = None, None
            if request.purchase_line:
                product = request.purchase_line.product.id
                supplier = request.purchase_line.purchase.party.id
            else:
                product = request.product.id
                supplier = request.party and request.party.id
            if not supplier:
                continue
            sup_delivery_time[product, supplier] = None

        prod_sup_ids = product_supplier_obj.search(
            cursor, user,
            ['OR', ] + [
                ['AND', ('product', '=', x[0]), ('party', '=', x[1])] \
                    for x in sup_delivery_time.iterkeys()
                ],
            context=context)
        for prod_sup in product_supplier_obj.browse(cursor, user, prod_sup_ids,
                                                    context=context):
            sup_delivery_time[prod_sup.product.id, prod_sup.party.id] = \
                prod_sup.delivery_time

        # Fetch data from existing requests
        existing_req = {}
        for request in requests:
            if request.purchase_line:
                product = request.purchase_line.product
                warehouse = request.purchase_line.purchase.warehouse
                purchase_date = request.purchase_line.purchase.purchase_date
                qty = line.quantity
                uom = line.unit
                supplier = line.purchase.party
            else:
                product = request.product
                warehouse = request.warehouse
                purchase_date = request.purchase_date
                qty = request.quantity
                uom = request.uom
                supplier = request.party

            delivery_time = sup_delivery_time.get((product.id, supplier.id))
            if delivery_time:
                supply_date = purchase_date + \
                    datetime.timedelta(delivery_time)
            else:
                supply_date = datetime.date.max

            existing_req.setdefault((product.id, warehouse.id), []).append(
                {'supply_date': supply_date, 'quantity': qty, 'uom': uom})

        for i in existing_req.itervalues():
            i.sort

        # Update new requests to take existing requests into account
        new_requests.sort(lambda r,s: cmp(r['supply_date'],s['supply_date']))
        for new_req in new_requests:
            for old_req in existing_req.get((new_req['product'].id,
                                             new_req['warehouse']), []):
                if old_req['supply_date'] <= new_req['supply_date']:
                    quantity = uom_obj.compute_qty(
                        cursor, user, old_req['uom'], old_req['quantity'],
                        new_req['uom'], context=context)
                    new_req['quantity'] = max(0.0, new_req['quantity'] - quantity)
                    old_req['quantity'] = uom_obj.compute_qty(
                        cursor, user, new_req['uom'],
                        max(0.0, quantity - new_req['quantity']),
                        old_req['uom'], context=context)
                else:
                    break

        for new_req in new_requests:
            if new_req['supply_date'] == datetime.date.max:
                new_req['supply_date'] = None
            if new_req['quantity'] > 0.0:
                new_req.update({'product': new_req['product'].id,
                                'party': new_req['party'] and new_req['party'].id,
                                'uom': new_req['uom'].id,
                                'company': new_req['company'].id
                                })
                request_obj.create(cursor, user, new_req, context=context)

    def get_supply_dates(self, cursor, user, product, context=None):
        """
        Return the minimal interval of earliest supply dates for a product.

        :param cursor: the database cursor
        :param user: the user id
        :param product: a BrowseRecord of the Product
        :param context: the context
        :return: a tuple with the two dates
        """
        product_supplier_obj = self.pool.get('purchase.product_supplier')

        min_date = None
        max_date = None
        today = datetime.date.today()

        for product_supplier in product.product_suppliers:
            supply_date, next_supply_date = product_supplier_obj.\
                    compute_supply_date(cursor, user, product_supplier,
                            date=today, context=context)
            if (not min_date) or supply_date < min_date:
                min_date = supply_date
            if (not max_date):
                max_date = next_supply_date
            if supply_date > min_date and supply_date < max_date:
                max_date = supply_date
            if next_supply_date < max_date:
                max_date = next_supply_date

        if not min_date:
            min_date = datetime.date.max
            max_date = datetime.date.max

        return (min_date, max_date)

    def compute_request(self, cursor, user, product, location_id, shortage_date,
                        product_quantity, company, order_point=None,
                        context=None):
        """
        Return the value of the purchase request which will answer to
        the needed quantity at the given date. I.e: the latest
        purchase date, the expected supply date and the prefered
        supplier.
        """
        uom_obj = self.pool.get('product.uom')
        product_supplier_obj = self.pool.get('purchase.product_supplier')

        supplier = None
        on_time = False
        today = datetime.date.today()
        max_quantity = order_point and order_point.max_quantity or 0.0
        for product_supplier in product.product_suppliers:
            supply_date = product_supplier_obj.compute_supply_date(cursor, user,
                    product_supplier, date=today, context=context)[0]
            sup_on_time = supply_date <= shortage_date
            if not supplier:
                supplier = product_supplier.party
                on_time = sup_on_time
                continue

            if not on_time and sup_on_time:
                supplier = product_supplier.party
                on_time = sup_on_time

        if supplier and product_supplier.delivery_time:
            purchase_date = product_supplier_obj.compute_purchase_date(cursor,
                    user, product_supplier, shortage_date, context=context)
        else:
            purchase_date = today

        quantity = uom_obj.compute_qty(cursor, user, product.default_uom,
                max_quantity - product_quantity, product.purchase_uom,
                context=context)

        return {'product': product,
                'party': supplier and supplier or None,
                'quantity': quantity,
                'uom': product.purchase_uom,
                'purchase_date': purchase_date,
                'supply_date': shortage_date,
                'stock_level': product_quantity,
                'company': company,
                'warehouse': location_id,
                }

    def get_shortage(self, cursor, user, location_id, product_id, min_date,
                     max_date, min_date_qty, order_point=None, context=None):
        """
        Return between min_date and max_date  the first date where the
            stock quantity is less than the minimal quantity and
            the smallest stock quantity in the interval
            or None if there is no date where stock quantity is less than
            the minimal quantity
        The minimal quantity comes from the order_point or is zero

        :param cursor: the database cursor
        :param user: the user id
        :param location_id: the stock location id
        :param produc_id: the product id
        :param min_date: the minimal date
        :param max_date: the maximal date
        :param min_date_qty: the stock quantity at the minimal date
        :param order_point: a BrowseRecord of the Order Point
        :param context: the context
        :return: a tuple with the date and the quantity
        """
        product_obj = self.pool.get('product.product')

        res_date = None
        res_qty = None

        min_quantity = order_point and order_point.min_quantity or 0.0

        current_date = min_date
        current_qty = min_date_qty
        while current_date <= max_date:
            if current_qty < min_quantity:
                if not res_date:
                    res_date = current_date
                if (not res_qty) or (current_qty < res_qty):
                    res_qty = current_qty

            local_context = context and context.copy() or {}
            local_context['stock_date_start'] = current_date
            local_context['stock_date_end'] = current_date
            res = product_obj.products_by_location(
                cursor, user, [location_id],
                [product_id], with_childs=True, skip_zero=False,
                context=context)
            for qty in res.itervalues():
                current_qty += qty
            if current_date == datetime.date.max:
                break
            current_date += datetime.timedelta(1)

        return (res_date, res_qty)

PurchaseRequest()


class CreatePurchaseAsk(WizardOSV):
    _name = 'stock.purchase_request.create_purchase.ask'
    party = fields.Many2One('relationship.party', 'Supplier', readonly=True)
    company = fields.Many2One('company.company', 'Company', readonly=True)
    payment_term = fields.Many2One(
        'account.invoice.payment_term', 'Payment Term', required=True)

CreatePurchaseAsk()

class CreatePurchase(Wizard):
    'Create Purchase'
    _name = 'stock.purchase_request.create_purchase'

    states = {

        'init': {
            'result': {
                'type': 'action',
                'action': '_compute_purchase',
                'state': 'choice',
                },
            },

        'choice': {
            'result': {
                'type': 'choice',
                'next_state': '_check_payment_term',
                },
            },

        'ask_user': {
            'actions': ['_set_default'],
            'result': {
                'type': 'form',
                'object': 'stock.purchase_request.create_purchase.ask',
                'state': [
                    ('end', 'Cancel', 'tryton-cancel'),
                    ('choice', 'Continue', 'tryton-ok', True),
                    ],
                },
            },

        'create': {
            'result': {
                'type': 'action',
                'action': '_create_purchase',
                'state': 'end',
                },
            },

        }

    def _set_default(self, cursor, user, data, context=None):

        if not data.get('party_wo_pt'):
            return {}
        party, company = data['party_wo_pt'].pop()
        return {'party': party,'company': company}

    def _check_payment_term(self, cursor, user, data, context=None):
        party_obj = self.pool.get('relationship.party')
        if 'purchases' not in data:
            return 'end'
        form = data['form']
        if form.get('payment_term') and form.get('party') and \
                form.get('company'):
            for key, val in data['purchases'].iteritems():
                if (key[0], key[1]) == (form['party'],
                                        form['company']):
                    val['payment_term'] = form['payment_term']
            local_context = context and context.copy() or {}
            local_context['company'] = form['company']
            party_obj.write(
                cursor, user, form['party'],
                {'supplier_payment_term': form['payment_term']}, context=local_context)
        if data.get('party_wo_pt'):
            return 'ask_user'
        return 'create'

    def _compute_purchase(self, cursor, user, data, context=None):
        request_obj = self.pool.get('stock.purchase_request')
        requests = request_obj.browse(cursor, user, data['ids'], context=context)
        purchases = {}
        for request in requests:
            if (not request.party) or request.purchase_line:
                continue
            key = (request.party.id, request.company.id, request.warehouse.id)

            if key not in purchases:
                purchase = {
                    'company': request.company.id,
                    'party': request.party.id,
                    'purchase_date': request.purchase_date or datetime.date.today(),
                    'payment_term': request.party.supplier_payment_term and \
                        request.party.supplier_payment_term.id or None,
                    'warehouse': request.warehouse.id,
                    'currency': request.company.currency.id,
                    'lines': [],
                    }
                purchases[key] = purchase
            else:
                purchase = purchases[key]

            purchase['lines'].append({
                'product': request.product.id,
                'unit': request.uom.id,
                'quantity': request.quantity,
                'request': request.id,
                })
            if request.purchase_date:
                if not purchase['purchase_date']:
                    purchase['purchase_date'] = request.purchase_date
                else:
                    purchase['purchase_date'] = min(purchase['purchase_date'],
                                                    request.purchase_date)
            data['purchases'] = purchases
            data['party_wo_pt'] = set(
                k[:2] for k in purchases if not purchases[k]['payment_term'])
        return {}

    def _create_purchase(self, cursor, user, data, context=None):
        request_obj = self.pool.get('stock.purchase_request')
        purchase_obj = self.pool.get('purchase.purchase')
        product_obj = self.pool.get('product.product')
        party_obj = self.pool.get('relationship.party')
        line_obj = self.pool.get('purchase.line')
        created_ids = []
        party_ids = []
        product_ids = []
        # collect  product names
        for purchase in data['purchases'].itervalues():
            party_ids.append(purchase['party'])
            for line in purchase['lines']:
                product_ids.append(line['product'])
        id2product = dict((p.id, p) for p in product_obj.browse(
                cursor, user, product_ids, context=context))
        id2party = dict((p.id, p) for p in party_obj.browse(
                cursor, user, party_ids, context=context))

        # create purchases, lines and update requests
        for purchase in data['purchases'].itervalues():
            party = id2party[purchase['party']]
            purchase_lines = purchase.pop('lines')
            purchase_id = purchase_obj.create(
                cursor, user, purchase, context=context)
            created_ids.append(purchase_id)
            for line in purchase_lines:
                product = id2product[line['product']]
                request_id = line.pop('request')

                line_values = self.compute_purchase_line(
                    cursor, user, line, purchase_id, purchase, product,
                    party, context=context)
                line_id = line_obj.create(cursor, user, line, context=context)
                request_obj.write(
                    cursor, user, request_id, {'purchase_line': line_id},
                    context=context)

        return {}

    def compute_purchase_line(self, cursor, user, line, purchase_id, purchase,
                             product, party, context=None):
        party_obj = self.pool.get('relationship.party')
        product_obj = self.pool.get('product.product')
        line['purchase'] = purchase_id
        line['description'] = product.name
        local_context = context.copy()
        local_context['uom'] = line['unit']
        local_context['supplier'] = purchase['party']
        local_context['currency'] = purchase['currency']
        product_price = product_obj.get_purchase_price(
            cursor, user, [line['product']], line['quantity'],
            context=local_context)[line['product']]
        line['unit_price'] = product_price

        taxes = []
        for tax in product.supplier_taxes:
            if 'supplier_' + tax.group.code in party_obj._columns \
                    and party['supplier_' + tax.group.code]:
                taxes.append(
                        party['supplier_' + tax.group.code].id)
                continue
            taxes.append(tax.id)
        line['taxes'] = [('add', taxes)]
        return line

CreatePurchase()
