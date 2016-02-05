# -*- coding: utf-8 -*-

from pprint import pprint
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
from dlstats.fetchers._commons import Fetcher, Datasets, Providers, SeriesIterator


logger = logging.getLogger(__name__)

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

def retry(tries=1, sleep_time=2):
    """Retry calling the decorated function
    :param tries: number of times to try
    :type tries: int
    """
    def try_it(func):
        def f(*args,**kwargs):
            attempts = 0
            while True:
                try:
                    return func(*args,**kwargs)
                except Exception as e:
                    attempts += 1
                    if attempts > tries:
                        raise e
                    time.sleep(sleep_time)
        return f
    return try_it

class WorldBankAPI(Fetcher):
    def __init__(self, db=None):
        super().__init__(provider_name='WB2',  db=db)
        self.provider = Providers(name=self.provider_name,
                                 long_name='World Bank',
                                 version=VERSION,
                                 region='world',
                                 website='http://www.worldbank.org/',
                                 fetcher=self)
        self.provider.update_database()
        self.api_url = 'http://api.worldbank.org/v2/'
        self.requests_client = requests.Session()
        self.blacklist = {'15': ['TOT']}
        self.whitelist = ['1', '15']

    @retry(tries=2, sleep_time=2)
    def download_or_raise(self, url, params={}):
        request = self.requests_client.get(url, params=params)
        request.raise_for_status()
        return request

    def download_json(self, url, parameters={}):
        per_page = 30000
        payload = {'format': 'json', 'per_page': per_page}
        payload.update(parameters)
        request = self.download_or_raise(self.api_url + url, params=payload)
        first_page = request.json()
        number_of_pages = int(first_page[0]['pages'])
        for page in range(1,number_of_pages+1):
            if page != 1:
                payload = {'format': 'json', 'per_page': per_page, 'page': page}
                request = self.download_or_raise(self.api_url + url, params=payload)
                request.raise_for_status()
            yield request.json()

    def download_indicator(self, country_code, indicator_code):
        for page in self.download_json('/'.join(['countries',
                                                 country_code,
                                                 'indicators',
                                                 indicator_code])):
            yield page

    def datasets_list(self):
        output = []
        for page in self.download_json('sources'):
            for source in page[1]:
                output.append(source['id'])
        return output

    def datasets_long_list(self):
        output = []
        for page in self.download_json('sources'):
            for source in page[1]:
                output.append((source['id'], source['name']))
        return output

    @property
    def available_countries(self):
        output = OrderedDict()
        for page in self.download_json('countries'):
            for source in page[1]:
                output[source['id']] = source['name']
        return output

    def series_list(self,dataset_code):
        output = []
        for page in self.download_json('/'.join(['sources',
                                                 dataset_code,
                                                 'indicators'])):
            for source in page[1]:
                output.append(source['id'])
        return output

    def build_data_tree(self, force_update=False):

        categories = []
        for source in self.datasets_long_list():
            categories.append({'name': source[1],
                               'category_code': source[0],
                               'datasets': [{'name': source[1],
                                             'dataset_code': source[0]}]})

        return categories

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
        result = dataset.update_database()
        end = time.time() - start
        logger.info("upsert dataset[%s] - END - time[%.3f seconds]" % (dataset_code, end))

        return result

    def load_datasets_first(self):
        start = time.time()
        logger.info("first load fetcher[%s] - START" % (self.provider_name))
        for dataset_code in self.datasets_list():
            self.upsert_dataset(dataset_code)
        end = time.time() - start
        logger.info("first load fetcher[%s] - END - time[%.3f seconds]" % (self.provider_name, end))


class WorldBankAPIData(SeriesIterator):

    def __init__(self, dataset):
        self.i=0
        self.dataset = dataset
        self.fetcher = self.dataset.fetcher
        self.dimension_list = dataset.dimension_list
        dimension_list = OrderedDict()
        dimension_list['country'] = self.fetcher.available_countries
        self.dimension_list.set_dict(dimension_list)
        self.dataset_code = self.dataset.dataset_code
        self.provider_name = self.fetcher.provider_name
        self.blacklisted_indicators = []
        if self.dataset_code in self.fetcher.blacklist:
            self.blacklisted_indicators = self.fetcher.blacklist[self.dataset_code]
        self.series_listed = self.fetcher.series_list(self.dataset_code) 
        self.series_to_process = list(set(self.series_listed) - set(self.blacklisted_indicators))
        self.countries_to_process = []

    def __iter__(self):
        return self

    def __next__(self):
        #TODO: Check for NaNs
        series = {}

        if not self.countries_to_process:
            if not self.series_to_process:
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
            self.release_date = page[0]['lastupdated']
            for point in page[1]:
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
            for point in page[1]:
                dates_and_values.append((point['date'],point['value']))

        series['dates_and_values'] = dates_and_values
        return self.build_series(series)

    def build_series(self, series):

        dates_and_values = series.pop('dates_and_values')

        if series['frequency'] == 'A':
            key_function = key_yearly
        elif series['frequency'] == 'M':
            key_function = key_monthly

        dates_and_values = sorted(dates_and_values, key=key_function)

        values = []
        for point in dates_and_values:
            value = {
                'attributes': None,
                'release_date': datetime.datetime.strptime(self.release_date, '%Y-%m-%d'),
                'value': str(point[1]) or 'NaN',
                'ordinal': pandas.Period(point[0],
                                         freq=series['frequency']).ordinal,
                'period': point[0],
                'period_o': point[0]
            }
            values.append(value)

        series['provider_name'] = self.provider_name
        series['dataset_code'] = self.dataset_code
        series['key'] = "%s.%s" % (self.current_series, self.current_country)
        series['name'] = series['key']
        series['values'] = values
        series['start_date'] = pandas.Period(dates_and_values[0][0],
                                            freq=series['frequency']).ordinal
        series['end_date'] = pandas.Period(dates_and_values[-1][0],
                                          freq=series['frequency']).ordinal
        series['attributes'] = None
        series['dimensions'] = {'country': self.current_country}

        self.i += 1

        return series


if __name__ == "__main__":

    logging.basicConfig(level=logging.DEBUG,
                        filename="wbapi.log",
                        #filemode="w+",
                        format='line:%(lineno)d - %(asctime)s %(name)s: [%(levelname)s] - [%(process)d] - [%(module)s] - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')

    import sys
    import os
    import tempfile
    print("WARNING : run main for testing only", file=sys.stderr)
    try:
        import requests_cache
        cache_filepath = os.path.abspath(os.path.join(tempfile.gettempdir(), 'dlstats_cache'))
        requests_cache.install_cache(cache_filepath, backend='sqlite', expire_after=None)#=60 * 60) #1H
        print("requests cache in %s" % cache_filepath)
    except ImportError:
        pass

    wb = WorldBankAPI()
    #for d in wb.datasets_long_list():
        #print(d)

    #wb.build_data_tree()
    wb.build_data_tree()
