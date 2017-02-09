# -*- coding: utf-8 -*-
# Copyright 2014-2016 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from itertools import islice
from openerp.osv import fields
from openerp.addons.connector.exception import MappingError
from openerp.addons.connector.unit.mapper import mapping, only_create
from ..backend import salesforce_backend
from ..unit.binder import SalesforceBinder
from ..unit.importer_synchronizer import (SalesforceDelayedBatchSynchronizer,
                                          SalesforceDirectBatchSynchronizer,
                                          SalesforceImportSynchronizer,
                                          ImportSkipReason,
                                          import_record)
from ..unit.rest_api_adapter import SalesforceRestAdapter
from ..unit.mapper import PriceMapper
_logger = logging.getLogger(__name__)

MAX_QUERY_OPP = 5000


@salesforce_backend
class SalesforceOpportunityImporter(SalesforceImportSynchronizer):
    _model_name = 'connector.salesforce.opportunity'

    def _deactivate(self):
        raise NotImplementedError(
            'Deactivation of sales order is not supported.'
        )

    def _before_import(self):
        """Hook called before Salesforce opportunity import
        to ensure product and pricelist consistency.
        """
        assert self.salesforce_record
        # We systematiquely reimport contacts
        # before creating opportunity to ensure
        # coherence and as quering if contact was updated
        # will cost more number of REST calls
        import_record(
            self.session,
            'connector.salesforce.account',
            self.backend_record.id,
            self.salesforce_record['AccountId']
        )

    def _after_import(self, binding):
        """Hook called after Salesforce opportunity import
        To automatically trigger opportunity items import
        """
        binding.ensure_one()
        items_to_import = self.backend_adapter.get_opportunity_items_ids(
            binding.salesforce_id
        )
        for item_id in items_to_import:
            import_record(
                self.session,
                'connector.salesforce.opportunity.line.item',
                self.backend_record.id,
                item_id
            )

    def _must_skip(self):
        """Return an `ImportSkipReason` based on binding.
        If a binding exists we skip the import
        """
        assert self.salesforce_id
        if self.binder.to_openerp(self.salesforce_id):
            return ImportSkipReason(should_skip=True,
                                    reason='Already imported')
        return ImportSkipReason(should_skip=False, reason=None)


@salesforce_backend
class SalesforceDirectBatchOpportunityImporter(
        SalesforceDirectBatchSynchronizer):
    _model_name = 'connector.salesforce.opportunity'


@salesforce_backend
class SalesforceDelayedBatchOpportunityImporter(
        SalesforceDelayedBatchSynchronizer):
    _model_name = 'connector.salesforce.opportunity'


@salesforce_backend
class SalesforceOpportunityAdapter(SalesforceRestAdapter):
    _model_name = 'connector.salesforce.opportunity'
    _sf_type = 'Opportunity'

    def _get_update_soql(self):
        """"Return SOQL to be used to query Won opportunites"""
        return ("SELECT Id FROM Opportunity WHERE Id "
                "IN (%s) AND IsWon = TRUE")

    def _get_query_item_soql(self):
        """"Return SOQL to be used to query related opportunity items"""
        return "SELECT Id FROM OpportunityLineItem WHERE OpportunityId = '%s'"

    def get_updated(self, start_datetime_str=None, end_datetime_str=None):
        """Override get updated to only fetch Won opportunites
        For more details have a look at :
        :py:class:`..unit.importer_synchronizer.SalesforceImportSynchronizer`
        """
        # we prefer to use standard SF getUpdated as it as a lot of
        # subtilites depending on model and redo a call
        full_result = super(SalesforceOpportunityAdapter, self).get_updated(
            start_datetime_str=start_datetime_str,
            end_datetime_str=end_datetime_str
        )
        while True:
            # is sliced does not raise an StopIteration error
            # but will instead provide an empty list
            sliced_ids = islice(full_result, 0, MAX_QUERY_OPP)
            sliced_ids = ["'%s'" % x for x in sliced_ids]
            if not sliced_ids:
                break
            query = self._get_update_soql()
            res = self.query(query, ', '.join(sliced_ids))
            for record in res['records']:
                yield record['Id']

    def get_opportunity_items_ids(self, salesforce_opp_id):
        """Return related opportunity items related to current
        opportunity"""
        res = self.query(self._get_query_item_soql(), salesforce_opp_id)
        return (record['Id'] for record in res['records'])


@salesforce_backend
class SalesforceOpportunityMapper(PriceMapper):
    _model_name = 'connector.salesforce.opportunity'

    direct = [
        ('Name', 'origin')
    ]

    @only_create
    @mapping
    def backend_id(self, record):
        return {'backend_id': self.backend_record.id}

    @mapping
    def pricelist_id(self, record):
        """Fetch pricelist using backend configuration"""
        currency_id = self.get_currency_id(record)
        mapping = {rec.currency_id.id: rec.pricelist_version_id.id
                   for rec in self.backend_record.sf_entry_mapping_ids}
        price_list_version_id = mapping.get(currency_id)
        if not price_list_version_id:
            raise MappingError(
                'No pricelist version configuration done for '
                'currency %s and backend %s' % (
                    record.get('CurrencyIsoCode'),
                    self.backend_record.name
                )
            )
        model = self.session.env['product.pricelist.version']
        price_list_version_record = model.browse(
            price_list_version_id
        )
        return {'pricelist_id': price_list_version_record.pricelist_id.id}

    @only_create
    @mapping
    def date_order(self, record):
        return {'date_order': fields.date.today()}

    @mapping
    def adresses(self, record):
        sf_account_id = record.get('AccountId')
        if not sf_account_id:
            raise MappingError(
                'No Account provided in Opportunity %s' % record
            )
        account_binder = self.unit_for(
            SalesforceBinder,
            model='connector.salesforce.account'
        )
        account = account_binder.to_openerp(record['AccountId'])
        if not account:
            raise MappingError(
                'Account %s does not exist' % record['AccountId']
            )
        partner_shipping_id = account.openerp_id.id
        if account.sf_shipping_partner_id:
            partner_shipping_id = account.sf_shipping_partner_id.id
        return {
            'partner_id': account.openerp_id.id,
            'partner_invoice_id': account.openerp_id.id,
            'partner_shipping_id': partner_shipping_id
        }

    @only_create
    @mapping
    def sales_team_id(self, record):
        return {'section_id': self.backend_record.sf_sales_team_id.id}

    def finalize(self, map_record, values):
        """Apply required on change on generated SO"""
        # We do not want to depends on connector ecommerce
        # only to have access to existing SaleOrderMapper
        # So we run `onchange` on a simplified manner

        # we use old api to avoid compatibility issues
        so_model = self.session.pool['sale.order']
        changed_values = so_model.onchange_partner_id(
            self.session.cr,
            self.session.uid,
            [],
            values['partner_id'],
            self.session.context
        )
        exclude_keys = values.keys()
        for key, val in changed_values['value'].iteritems():
            if key not in exclude_keys:
                values[key] = val
        return values


@salesforce_backend
class SalesforceOpportunityLineItemImporter(SalesforceImportSynchronizer):
    _model_name = 'connector.salesforce.opportunity.line.item'

    def _to_deactivate(self):
        """Hook to check if record must be deactivated"""
        return False

    def _deactivate(self):
        pass

    def _before_import(self):
        """Hook called before importing a Salesforce opportunity line
        to ensure product and pricelist are coherent"""
        assert self.salesforce_record
        if not self.salesforce_record.get('Product2Id'):
            return
        product_binder = self.unit_for(
            SalesforceBinder,
            model='connector.salesforce.product'
        )
        product_id = product_binder.to_openerp(
            self.salesforce_record['Product2Id']
        )
        if not product_id:
            if self.backend_record.sf_product_master == 'sf':
                import_record(
                    self.session,
                    'connector.salesforce.product',
                    self.backend_record.id,
                    self.salesforce_record['Product2Id']
                )


@salesforce_backend
class SalesforceOpportunityLineItemAdapter(SalesforceRestAdapter):
    _model_name = 'connector.salesforce.opportunity.line.item'
    _sf_type = 'OpportunityLineItem'

    def _get_product_soql(self):
        return ("SELECT PricebookEntry.Product2Id "
                "FROM OpportunityLineItem where Id = '%s'")

    def _get_products(self, salesforce_line_uuid):
        res = self.query(self._get_product_soql(), salesforce_line_uuid)
        return [
            record['PricebookEntry']['Product2Id'] for record in res['records']
            if record.get('PricebookEntry', {}).get('Product2Id')
        ]


@salesforce_backend
class SalesforceOpportunityLineItemMapper(PriceMapper):
    _model_name = 'connector.salesforce.opportunity.line.item'

    direct = [
        ('Discount', 'discount')
    ]

    @only_create
    @mapping
    def backend_id(self, record):
        return {'backend_id': self.backend_record.id}

    @mapping
    def name(self, record):
        # SF Sale description is limited to 255 char
        name = record.get('Description')
        if not name:
            return {}
        return {'name': name}

    @mapping
    def product_id(self, record):
        backend_adapter = self.environment.get_connector_unit(
            SalesforceOpportunityLineItemAdapter)
        sf_product_uuid = backend_adapter._get_products(record['Id'])
        if not sf_product_uuid:
            return {'product_id': False}
        product_binder = self.unit_for(
            SalesforceBinder,
            model='connector.salesforce.product'
        )
        bind_product = product_binder.to_openerp(
            sf_product_uuid[0]
        )
        if not bind_product:
            raise MappingError(
                'Product is not available in ERP for record %s' % record
            )
        return {'product_id': bind_product.openerp_id.id}

    @mapping
    def price_and_qty(self, record):
        sf_price = record.get('ListPrice')
        if not sf_price:
            raise MappingError(
                'No ListPrice given for Opportunity Item %s' % record
            )
        quantity = record.get('Quantity')
        if not quantity:
            quantity = 1.0
        return {
            'price_unit': sf_price,
            'product_uom_qty': quantity,
        }

    @mapping
    def order_id(self, record):
        sf_opportunity_uuid = record.get('OpportunityId')
        if not sf_opportunity_uuid:
            raise MappingError(
                'No OpportunityId for record %s' % record
            )
        opportunity_binder = self.unit_for(
            SalesforceBinder,
            model='connector.salesforce.opportunity'
        )
        bind_opportunity = opportunity_binder.to_openerp(
            sf_opportunity_uuid
        )
        if not bind_opportunity:
            raise MappingError(
                'No Opportunity for item %s' % record
            )
        return {'order_id': bind_opportunity.openerp_id.id}

    def finalize(self, map_record, values):
        """Call afer item mapping to call the on change on
        generated SO lines
        """
        # We do not want to depends on connector ecommerce
        # only to have access to existing SaleOrderMapper
        # So we run `onchange` on a simplified manner
        so_line_model = self.session.pool['sale.order.line']
        sale_order = self.session.env['sale.order'].browse(
            values['order_id']
        )
        changed_values = so_line_model.product_id_change(
            self.session.cr,
            self.session.uid,
            [],
            sale_order.pricelist_id.id,
            values['product_id'],
            partner_id=sale_order.partner_id.id,
            qty=values['product_uom_qty'],
            date_order=sale_order.date_order,
            fiscal_position=sale_order.fiscal_position.id,
            context=self.session.context
        )
        exclude_keys = values.keys()
        for key, val in changed_values['value'].iteritems():
            if key not in exclude_keys:
                values[key] = val
        return values