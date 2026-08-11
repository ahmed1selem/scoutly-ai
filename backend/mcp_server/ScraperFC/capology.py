from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException
import pandas as pd
import requests
from bs4 import BeautifulSoup
from io import StringIO

from ScraperFC.scraperfc_exceptions import InvalidCurrencyException, InvalidLeagueException, \
    InvalidYearException
from ScraperFC.utils import get_module_comps

comps = get_module_comps("CAPOLOGY")

class Capology():

    # ==============================================================================================
    def __init__(self) -> None:
        self.valid_currencies = ['eur', 'gbp', 'usd']

    # ==============================================================================================
    def _webdriver_init(self) -> None:
        """ Initializes a new webdriver

        Deliberately NOT headless. Capology sits behind Cloudflare, which serves
        a "Just a moment... performing security verification" interstitial to
        headless Chrome and never resolves it — the page has no table and no
        season dropdown, which surfaced downstream as
        ``'NoneType' object has no attribute 'find_all'``. A visible window
        clears the same challenge in under six seconds.

        This only runs in the offline cache build, never while serving a
        request, so a browser window appearing is acceptable. Set
        ``CAPOLOGY_HEADLESS=1`` to force the old behaviour.
        """
        import os

        options = Options()
        if os.getenv('CAPOLOGY_HEADLESS', '').strip() in ('1', 'true', 'True'):
            options.add_argument('--headless=new')
        options.add_argument('--incognito')
        # Cloudflare fingerprints the automation banner, so drop it.
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        # The original code set
        #   prefs = {'profile.managed_default_content_settings.images': 2}
        # to skip image downloads. That single setting is what Cloudflare
        # fingerprints: measured A/B against the live site, a visible window
        # passes the challenge with images on and is blocked with them off,
        # while --incognito makes no difference either way. The bandwidth was
        # not worth every wage build returning zero rows.
        self.driver = webdriver.Chrome(options=options)

    # ==============================================================================================
    def _soup_via_driver(self, url: str, wait_for: str = 'nav-submenu2', timeout: int = 40):
        """ Fetch a Capology page through the browser rather than ``requests``.

        The season-dropdown lookups used to call ``requests.get`` directly, which
        Cloudflare answers with a 403 challenge page. Every wage build therefore
        failed before it reached the part that already had a working driver.
        """
        import time

        own_driver = not hasattr(self, 'driver') or self.driver is None
        if own_driver:
            self._webdriver_init()
        try:
            self.driver.get(url)
            deadline = time.time() + timeout
            while time.time() < deadline:
                soup = BeautifulSoup(self.driver.page_source, 'html.parser')
                if soup.find('select', {'id': wait_for}) is not None:
                    return soup
                time.sleep(2)
            # Return whatever we have; the caller raises a clearer error than a
            # bare None dereference.
            return BeautifulSoup(self.driver.page_source, 'html.parser')
        finally:
            if own_driver:
                self._webdriver_close()
                self.driver = None

    # ==============================================================================================
    def _webdriver_close(self) -> None:
        """ Closes and quits the Selenium WebDriver instance.
        """
        self.driver.close()
        self.driver.quit()

    # ==============================================================================================
    def get_league_url(self, league: str) -> str:
        """ Returns the URL for the requested league

        :param league: .. include:: ./arg_docstrings/league.rst
        :type league: str
        :raises TypeError: If the league is not a string.
        :raises InvalidLeagueException: If the league is not valid for this module.
        :return: League URL
        :rtype: str
        """
        if not isinstance(league, str):
            raise TypeError('`league` must be a string.')
        if league not in comps.keys():
            raise InvalidLeagueException(league, 'Capology', list(comps.keys()))

        return f'https://www.capology.com/{comps[league]["CAPOLOGY"]}/salaries/'

    # ==============================================================================================
    def get_valid_seasons(self, league: str) -> list[str]:
        """ Returns valid season strings for the chosen league

        :param league: .. include:: ./arg_docstrings/league.rst
        :type league: str
        :raises TypeError: If any of the parameters are the wrong type.
        :raises InvalidLeagueException: If the league is not valid for this module.
        :return: List of valid year strings for this league
        :rtype: list[str]
        """
        if not isinstance(league, str):
            raise TypeError('`league` must be a string.')
        if league not in comps.keys():
            raise InvalidLeagueException(league, 'Capology', list(comps.keys()))

        soup = self._soup_via_driver(self.get_league_url(league))
        dropdown = soup.find('select', {'id': 'nav-submenu2'})
        if dropdown is None:
            raise RuntimeError(
                f'Capology returned no season dropdown for {league}. This is '
                'normally the Cloudflare interstitial — check that Chrome is '
                'running with a visible window (CAPOLOGY_HEADLESS must not be set).'
            )
        seasons = [x.text for x in dropdown.find_all('option', value=True)]

        return seasons

    # ==============================================================================================
    def get_season_url(self, year: str, league: str) -> str:
        """ Gets URL to chosen year of league

        :param year: .. include:: ./arg_docstrings/year_capology.rst
        :type year: str
        :param league: .. include:: ./arg_docstrings/league.rst
        :type league: str
        :raises TypeError: If any of the parameters are the wrong type.
        :raises InvalidYearException: If the year is not valid for the league.
        :return: Season URL
        :rtype: str
        """
        if not isinstance(year, str):
            raise TypeError('`year` must be a string.')
        valid_seasons = self.get_valid_seasons(league)
        if year not in valid_seasons:
            raise InvalidYearException(year, league, valid_seasons)

        soup = self._soup_via_driver(self.get_league_url(league))
        dropdown = soup.find('select', {'id': 'nav-submenu2'})
        if dropdown is None:
            raise RuntimeError(
                f'Capology returned no season dropdown for {league} (Cloudflare).'
            )
        year_dropdown_tags = dropdown.find_all('option', value=True)
        value = [x['value'] for x in year_dropdown_tags if x.text == year][0]

        return f'https://capology.com{value}'

    # ==============================================================================================
    def scrape_salaries(self, year: str, league: str, currency: str) -> pd.DataFrame:
        """ Scrapes player salaries for the given league season.

        :param year: .. include:: ./arg_docstrings/year_capology.rst
        :type year: str
        :param league: .. include:: ./arg_docstrings/league.rst
        :type league: str
        :param currency: The currency for the returned salaries. Options are "eur" for Euro,
            "gbp" for British Pound, and "USD" for US Dollar
        :type currency: str
        :raises TypeError: If any of the parameters are the wrong type.
        :raises InvalidCurrencyException: If ``currency`` is not a valid choice.
        :return: The salaries of all players in the given league season
        :rtype: pd.DataFrame
        """
        if not isinstance(currency, str):
            raise TypeError('`currency` must be a string.')
        if currency not in self.valid_currencies:
            raise InvalidCurrencyException()

        self._webdriver_init()
        try:
            self.driver.get(self.get_season_url(year, league))

            # -- Show 100 players per page ---------------------------------------------------------
            done = False
            while not done:
                try:
                    show_100_btn = WebDriverWait(self.driver, 10)\
                        .until(EC.element_to_be_clickable((By.LINK_TEXT, '100')))
                    self.driver.execute_script('arguments[0].click()', show_100_btn)
                    done = True
                except StaleElementReferenceException:
                    pass

            # -- Select the currency ---------------------------------------------------------------
            currency_btn = WebDriverWait(self.driver, 10)\
                .until(EC.element_to_be_clickable((By.ID, f'btn_{currency}')))
            self.driver.execute_script('arguments[0].click()', currency_btn)
            print(f'Changed currency to {currency}.')

            # -- Visit all of the player pages and get the salary tables ---------------------------
            pages_visited = list()
            df = pd.DataFrame()
            current_page = BeautifulSoup(self.driver.page_source, "html.parser")\
                .find("li", {"class": "page-item active"}).text  # type: ignore
            while current_page not in pages_visited:
                pages_visited.append(current_page)

                # Get the salary table from the current page and and it df
                table_el = self.driver.find_element(By.ID, "table")
                temp = pd.read_html(StringIO(table_el.get_attribute("outerHTML")))[0]
                df = pd.concat([df, temp], axis=0, ignore_index=True)

                # Go the next page
                next_btn = self.driver.find_element(By.LINK_TEXT, "Next")
                self.driver.execute_script('arguments[0].click()', next_btn)
                current_page = BeautifulSoup(self.driver.page_source, "html.parser")\
                    .find("li", {"class": "page-item active"}).text  # type: ignore

            # Get the cleaned column names ---------------------------------------------------------
            table_header = table_el.find_element(By.TAG_NAME, "thead")
            table_header_rows = table_header.find_elements(By.TAG_NAME, "tr")

            level0_cols = list()
            for th in table_header_rows[0].find_elements(By.TAG_NAME, "th"):
                col_name = th.find_element(By.TAG_NAME, "div").text
                col_span = int(th.get_attribute("colspan"))  # type: ignore
                _ = [level0_cols.append(col_name) for _ in range(col_span)]  # type: ignore

            level1_cols = list()
            for th in table_header_rows[1].find_elements(By.TAG_NAME, "th"):
                col_name = (
                    th.get_attribute("data-field").upper()  # type: ignore
                    if th.get_attribute("class") == "hide"
                    else th.find_element(By.TAG_NAME, "div").text
                )
                level1_cols.append(col_name)

            # Sometimes the level 0 header row has less colspans than cols in the 2nd header row
            while len(level0_cols) < len(level1_cols):
                level0_cols.append("")

            df.columns = pd.MultiIndex.from_arrays([level0_cols, level1_cols])

            return df
        finally:
            self._webdriver_close()

    # ==============================================================================================
    def scrape_payrolls(self, year: str, league: str, currency: str) -> pd.DataFrame:
        """ Deprecated. Use scrape_salaries() instead.
        """
        raise NotImplementedError(
            '`scrape_payrolls()` has been deprecated. Please use `scrape_salaries()` instead.'
        )
