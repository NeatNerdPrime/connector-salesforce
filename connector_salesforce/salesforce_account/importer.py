# -*- coding: utf-8 -*-
# Copyright 2014-2016 Camptocamp SA
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from openerp.addons.connector.exception import MappingError
from openerp.addons.connector.unit.mapper import mapping, only_create
from ..backend import salesforce_backend
from ..unit.importer_synchronizer import (SalesforceDelayedBatchSynchronizer,
                                          SalesforceDirectBatchSynchronizer,
                                          SalesforceImportSynchronizer)
from ..unit.rest_api_adapter import SalesforceRestAdapter
from ..unit.mapper import AddressMapper, PriceMapper


_logger = logging.getLogger(__name__)


@salesforce_backend
class SalesforceAccountImporter(SalesforceImportSynchronizer):
    _model_name = 'connector.salesforce.account'

    def _after_import(self, binding):
        """Hook that is called after Salesforce account import
        It maps Salesforce account data into child partner on Odoo
        """
        # Can be used in Mapper.finalize but
        # manage nested syntax when updating would have been a mess
        record_mapper = self.mapper
        shipping_add_data = record_mapper.map_shipping_address(
            self.salesforce_record,
            binding,
        )
        binding.write(shipping_add_data)


@salesforce_backend
class SalesforceDirectBatchAccountImporter(SalesforceDirectBatchSynchronizer):
    _model_name = 'connector.salesforce.account'


@salesforce_backend
class SalesforceDelayedBatchAccountImporter(
        SalesforceDelayedBatchSynchronizer):
    _model_name = 'connector.salesforce.account'


@salesforce_backend
class SalesforceAccountAdapter(SalesforceRestAdapter):
    _model_name = 'connector.salesforce.account'
    _sf_type = 'Account'


@salesforce_backend
class SalesforceAccountMapper(AddressMapper, PriceMapper):
    _model_name = 'connector.salesforce.account'

    direct = [
        ('Name', 'name'),
        ('BillingStreet', 'street'),
        ('BillingPostalCode', 'zip'),
        ('BillingCity', 'city'),
        ('Fax', 'fax'),
        ('Phone', 'phone'),
        # To support commonly installed VAT application
        ('VATNumber__c', 'vat'),
    ]

    def _prepare_shipping_address_data(self, record, partner_record):
        """Convert shipping address information to res.partner data dict"""
        data = {
            'name': record['Name'],
            'street': record['ShippingStreet'],
            'zip': record['ShippingPostalCode'],
            'city': record['ShippingCity'],
            'phone': record['Phone'],
            'parent_id': partner_record.openerp_id.id,
            'type': 'delivery',
            'customer': True,

        }
        country_id = self._country_id(record, 'ShippingCountryCode')
        data['country_id'] = country_id
        state_id = self._state_id(record,
                                  'ShippingState',
                                  'ShippingCountryCode')
        data['state_id'] = state_id
        return data

    def map_shipping_address(self, record, binding=None):
        """Manage the Salesforce account shipping address
        If no shipping address exists in Odoo it is created.
        If a shipping address already exists we update it.
        If no shipping data are present and a shipping adress exists
        it will be unactivated

        """
        if not binding:
            raise MappingError(
                'No binding found when mapping shipping address'
            )
        binding.ensure_one()
        current_partner = binding
        shipp_id = False
        shipp_fields = (field for field in record
                        if field.startswith('Shipping'))
        if any(record[field] for field in shipp_fields):
            if current_partner.sf_shipping_partner_id:
                shipp_id = current_partner.sf_shipping_partner_id.id
                self.session.env['res.partner'].write(
                    [shipp_id],
                    self._prepare_shipping_address_data(record,
                                                        current_partner)
                )
            else:
                shipp_id = self.session.env['res.partner'].create(
                    self._prepare_shipping_address_data(record,
                                                        current_partner)
                ).id
        else:
            if current_partner.sf_shipping_partner_id:
                self.session.env['res.partner'].write(
                    [current_partner.sf_shipping_partner_id.id],
                    {'active': False}
                )
        return {'sf_shipping_partner_id': shipp_id}

    @only_create
    @mapping
    def backend_id(self, record):
        return {'backend_id': self.backend_record.id}

    @only_create
    @mapping
    def is_company(self, record):
        return {'is_company': True}

    @mapping
    def country_id(self, record):
        country_id = self._country_id(record, 'BillingCountryCode')
        return {'country_id': country_id}

    @mapping
    def state_id(self, record):
        state_id = self._state_id(record, 'BillingState', 'BillingCountryCode')
        return {'state_id': state_id}

    @mapping
    def customer(self, record):
        return {'customer': True}

    @mapping
    def active(self, record):
        return {'active': True}

    @mapping
    def property_product_pricelist(self, record):
        """Map property pricelist using current backend setting"""
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
        pl_model = 'product.pricelist.version'
        price_list_version_record = self.session.env[pl_model].browse(
            price_list_version_id
        )
        return {'property_product_pricelist':
                price_list_version_record.pricelist_id.id}