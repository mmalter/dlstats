# -*- coding: utf-8 -*-

import tempfile
import logging
import os
import zipfile
import datetime
import time
import xlrd
import pandas
import requests
from collections import OrderedDict
from operator import itemgetter
from dlstats.fetchers._commons import Fetcher, Datasets, Providers


logger = logging.getLogger(__name__)
fmt = logging.Formatter('%(asctime)s %(message)s')
fh = logging.FileHandler("wbapi.log")
fh.setFormatter(fmt)
logger.setLevel(logging.DEBUG)
fh.setLevel(logging.DEBUG)
logger.addHandler(fh)

VERSION = 1

def key_monthly(point):
    "Key function for sorting dates of the format 2008M12"
    string_month = point[0]
    year, month = string_month.split('M')
    return int(year)*100+int(month)

def key_yearly(point):
    "Key function for sorting dates of the format 2008"
    string_year = point[0]
    return int(string_year)

class WorldBankAPI(Fetcher):
    def __init__(self, db=None):
        super().__init__(provider_name='WorldBank',  db=db)
        self.provider = Providers(name=self.provider_name,
                                 long_name='World Bank',
                                 version=VERSION,
                                 region='world',
                                 website='http://www.worldbank.org/',
                                 fetcher=self)
        self.api_url = 'http://api.worldbank.org/'
        self.requests_client = requests.Session()

    def download_json(self, url, parameters={}):
        per_page = 30000
        payload = {'format': 'json', 'per_page': per_page}
        payload.update(parameters)
        request = self.requests_client.get(self.api_url + url, params=payload)
        first_page = request.json()
        number_of_pages = int(first_page[0]['pages'])
        for page in range(1,number_of_pages+1):
            payload = {'format': 'json', 'per_page': per_page, 'page': page}
            request = self.requests_client.get(self.api_url + url, params=payload)
            yield request.json()[1]

    def download_indicator(self, country_code, indicator_code):
        for page in self.download_json('/'.join(['countries',
                                                 country_code,
                                                 'indicators',
                                                 indicator_code])):
            yield page

    def datasets_list(self):
        output = []
        for page in self.download_json('sources'):
            for source in page:
                output.append(source['id'])
        return output

    def datasets_long_list(self):
        output = []
        for page in self.download_json('sources'):
            for source in page:
                output.append((source['id'], source['name']))
        return output

    @property
    def available_countries(self):
        output = OrderedDict()
        for page in self.download_json('countries'):
            for source in page:
                output[source['id']] = source['name']
        return output

    def series_list(self,dataset_code):
        output = []
        for page in self.download_json('/'.join(['sources',
                                                 dataset_code,
                                                 'indicators'])):
            for source in page:
                output.append(source['id'])
        return output

    def build_data_tree(self, force_update=False):

        if self.provider.count_data_tree() > 1 and not force_update:
            return self.provider.data_tree
        for source in self.datasets_long_list:
            self.provider.add_category({'name': source[1],
                                        'category_code': source[0]})
            self.provider.add_dataset({'name': source[1],
                                       'dataset_code': source[0]})

    def upsert_dataset(self, dataset_code):
        start = time.time()
        logger.info("upsert dataset[%s] - START" % (dataset_code))
        for dataset_code_, name_ in self.datasets_long_list():
            if dataset_code_ == dataset_code:
                name = name_
                break

        dataset = Datasets(provider_name=self.provider_name,
                           dataset_code=dataset_code,
                           name=name,
                           last_update=datetime.datetime.now(),
                           fetcher=self)
        dataset.series.data_iterator = WorldBankAPIData(dataset)
        dataset.update_database()
        end = time.time() - start
        logger.info("upsert dataset[%s] - END - time[%.3f seconds]" % (dataset_code, end))

    def load_datasets_first(self):
        start = time.time()
        logger.info("first load fetcher[%s] - START" % (self.provider_name))
        for dataset_code in self.datasets_list():
            self.upsert_dataset(dataset_code)
        end = time.time() - start
        logger.info("first load fetcher[%s] - END - time[%.3f seconds]" % (self.provider_name, end))


class WorldBankAPIData(object):
    def __init__(self, dataset):
        self.dataset = dataset
        self.fetcher = self.dataset.fetcher
        self.dimension_list = dataset.dimension_list
        dimension_list = OrderedDict()
        dimension_list['country'] = self.fetcher.available_countries
        self.dimension_list.set_dict(dimension_list)
        self.dataset_code = self.dataset.dataset_code
        self.provider_name = self.fetcher.provider_name
        self.series_to_process = self.fetcher.series_list(self.dataset_code) 
        self.countries_to_process = []

    def __iter__(self):
        return self

    def __next__(self):
        #TODO: Check for NaNs
        series = {}
        if self.countries_to_process == []:
            if self.series_to_process == []:
                raise StopIteration()
            self.countries_to_process = list(self.fetcher.available_countries.keys())
            self.current_series = self.series_to_process.pop()
        self.current_country = self.countries_to_process.pop()
        logger.debug("Fetching the series {0} for the country {1}"
                     .format(self.current_series, self.current_country))
        # Only retrieve the first page to get more information about the series
        indicator = self.fetcher.download_indicator(self.current_country,
                                                    self.current_series)
        dates_and_values = []
        dates = []
        has_page = False
        for page in indicator:
            has_page = True
            for point in page:
                if len(point['date']) == 4:
                    series['frequency'] = 'A'
                if len(point['date']) == 7:
                    series['frequency'] = 'M'
                series['name'] = point['indicator']['value']
                break
            break
        if has_page == False:
            return self.__next__()
        # Then proceed with all the pages
        indicator = self.fetcher.download_indicator(self.current_country,
                                                    self.current_series)
        for page in indicator:
            for point in page:
                dates_and_values.append((point['date'],point['value']))
        if series['frequency'] == 'A':
            key_function = key_yearly
        elif series['frequency'] == 'M':
            key_function = key_monthly
        dates_and_values = sorted(dates_and_values, key=key_function)
        series['provider_name'] = self.provider_name
        series['dataset_code'] = self.dataset_code
        series['key'] = self.current_series + '.' + self.current_country
        series['values'] = [point[1] or 'NaN' for point in dates_and_values]
        series['start_date'] = pandas.Period(dates_and_values[0][0],
                                            freq=series['frequency']).ordinal
        series['end_date'] = pandas.Period(dates_and_values[-1][0],
                                          freq=series['frequency']).ordinal
        series['attributes'] = {}
        series['dimensions'] = {'country': self.current_country}
        return series


if __name__ == "__main__":
    wb = WorldBankAPI()
    wb.upsert_dataset('15')
