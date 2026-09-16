"""
DoneDeal Ireland Scraper
Extracts listings from DoneDeal.ie across all categories
"""
import asyncio
from urllib.parse import urljoin
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from apify import Actor

async def main():
    async with Actor:
        # Get input
        actor_input = await Actor.get_input() or {}
        start_urls = actor_input.get('startUrls', [])
        category = actor_input.get('category', 'cars')
        county = actor_input.get('county', '')
        price_min = actor_input.get('priceMin', 0)
        price_max = actor_input.get('priceMax', 0)
        max_results = actor_input.get('maxResults', 100)
        proxy_config = actor_input.get('proxyConfiguration', {'useApifyProxy': True})
        
        # Build start URL if not provided
        if not start_urls:
            base_url = f"https://www.donedeal.ie/{category}"
            start_urls = [{'url': base_url}]
        
        Actor.log.info(f"Starting scrape: category={category}, county={county}, max_results={max_results}")
        
        results_count = 0
        
        # Define request handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            nonlocal results_count
            
            if results_count >= max_results:
                return
            
            page = context.page
            url = context.request.url
            
            Actor.log.info(f"Scraping: {url}")
            
            try:
                # Apply playwright-stealth for Cloudflare bypass
                try:
                    from playwright_stealth import stealth_async
                    await stealth_async(page)
                    Actor.log.info("Stealth mode applied")
                except ImportError:
                    Actor.log.warning("playwright-stealth not available, continuing without it")
                
                # Wait for content to load
                await page.wait_for_load_state('networkidle', timeout=30000)
                
                # Additional wait for dynamic content
                await page.wait_for_timeout(3000)
                
                # Check if this is a listing detail page or search results
                is_detail = '/for-sale/' in url or '/cars-for-sale/' in url or '/ad/' in url
                
                if is_detail:
                    # Extract single listing
                    listing = await extract_listing_detail(page, url)
                    if listing and results_count < max_results:
                        await context.push_data(listing)
                        results_count += 1
                        Actor.log.info(f"Scraped listing: {listing.get('title', 'N/A')} [{results_count}/{max_results}]")
                else:
                    # Extract listings from search results
                    listings = await extract_listings_from_search(page, url)
                    
                    for listing in listings:
                        if results_count >= max_results:
                            break
                        if listing:
                            await context.push_data(listing)
                            results_count += 1
                            Actor.log.info(f"Scraped: {listing.get('title', 'N/A')} [{results_count}/{max_results}]")
                    
                    # Enqueue pagination if needed
                    if results_count < max_results:
                        await enqueue_pagination(context, page)
                        
            except Exception as e:
                Actor.log.error(f"Error scraping {url}: {str(e)}")
        
        # Create crawler with browser options for Cloudflare bypass
        crawler = PlaywrightCrawler(
            request_handler=request_handler,
            max_requests_per_crawl=max_results + 10,
            headless=True,
            browser_type='chromium',
            proxy_configuration=await Actor.create_proxy_configuration(proxy_config) if proxy_config else None,
        )
        
        # Run the crawler
        await crawler.run(start_urls)
        
        # Get environment info
        env = Actor.get_env()
        
        # Save task info
        await Actor.set_value('SAVED-TASK', {
            'actorId': env.get('actor_id'),
            'actorRunId': env.get('actor_run_id'),
            'defaultDatasetId': env.get('default_dataset_id'),
            'input': actor_input,
            'stats': {
                'results_scraped': results_count,
                'max_results': max_results,
            }
        })
        
        Actor.log.info(f"Scraping complete: {results_count} listings scraped")

async def extract_listing_detail(page, url):
    """Extract data from a single listing detail page"""
    try:
        title = await page.locator('h1').first.text_content(timeout=5000) or ''
        
        price_elem = await page.locator('[class*="price"], [class*="Price"]').first.text_content(timeout=3000) if await page.locator('[class*="price"], [class*="Price"]').count() > 0 else None
        price = price_elem.strip() if price_elem else 'Contact for Price'
        
        description = ''
        desc_selectors = ['[class*="description"]', '[class*="Description"]', 'article p']
        for selector in desc_selectors:
            if await page.locator(selector).count() > 0:
                description = await page.locator(selector).first.text_content(timeout=3000) or ''
                if description:
                    break
        
        location = ''
        loc_selectors = ['[class*="location"]', '[class*="county"]', '[class*="Location"]']
        for selector in loc_selectors:
            if await page.locator(selector).count() > 0:
                location = await page.locator(selector).first.text_content(timeout=3000) or ''
                if location:
                    break
        
        # Extract images
        images = []
        img_elements = await page.locator('img[src*="donedeal"], img[src*="cdn"]').all()
        for img in img_elements[:5]:  # Limit to 5 images
            src = await img.get_attribute('src')
            if src and 'http' in src:
                images.append(src)
        
        return {
            'title': title.strip(),
            'price': price,
            'location': location.strip(),
            'description': description.strip(),
            'url': url,
            'images': images,
            'category': extract_category_from_url(url),
            'listingId': url.split('/')[-1] if '/' in url else '',
            'datePosted': '',
            'seller': {},
            'specifications': {}
        }
    except Exception as e:
        Actor.log.error(f"Error extracting listing detail: {str(e)}")
        return None

async def extract_listings_from_search(page, url):
    """Extract multiple listings from search results page"""
    listings = []
    
    try:
        # Wait for listings to appear
        await page.wait_for_selector('a[href*="/for-sale/"], a[href*="/cars-for-sale/"], a[href*="/ad/"]', timeout=5000)
        
        # Find listing cards/links
        listing_links = await page.locator('a[href*="/for-sale/"], a[href*="/cars-for-sale/"], a[href*="/ad/"]').all()
        
        seen_urls = set()
        for link in listing_links[:20]:  # Process up to 20 per page
            try:
                href = await link.get_attribute('href')
                if not href or href in seen_urls:
                    continue
                seen_urls.add(href)
                
                full_url = urljoin(url, href)
                
                # Try to extract basic info from the card
                title_elem = await link.locator('h2, h3, [class*="title"], [class*="Title"]').first
                title = await title_elem.text_content(timeout=1000) if await link.locator('h2, h3, [class*="title"], [class*="Title"]').count() > 0 else 'Listing'
                
                price_elem = await link.locator('[class*="price"], [class*="Price"]').first
                price = await price_elem.text_content(timeout=1000) if await link.locator('[class*="price"], [class*="Price"]').count() > 0 else 'Contact for Price'
                
                loc_elem = await link.locator('[class*="location"], [class*="Location"]').first
                location = await loc_elem.text_content(timeout=1000) if await link.locator('[class*="location"], [class*="Location"]').count() > 0 else ''
                
                listings.append({
                    'title': title.strip() if title else 'Listing',
                    'price': price.strip() if price else 'Contact for Price',
                    'location': location.strip() if location else '',
                    'url': full_url,
                    'category': extract_category_from_url(full_url),
                    'listingId': full_url.split('/')[-1] if '/' in full_url else '',
                    'description': '',
                    'images': [],
                    'datePosted': '',
                    'seller': {},
                    'specifications': {}
                })
            except Exception as e:
                continue
                
    except Exception as e:
        Actor.log.warning(f"Error extracting search results: {str(e)}")
    
    return listings

async def enqueue_pagination(context, page):
    """Find and enqueue next page links"""
    try:
        next_selectors = [
            'a[rel="next"]',
            'a[class*="next"]',
            'a[class*="Next"]',
            'a:has-text("Next")',
            'button:has-text("Next")'
        ]
        
        for selector in next_selectors:
            if await page.locator(selector).count() > 0:
                next_link = await page.locator(selector).first
                href = await next_link.get_attribute('href')
                if href:
                    full_url = urljoin(context.request.url, href)
                    await context.add_requests([full_url])
                    Actor.log.info(f"Enqueued pagination: {full_url}")
                    break
    except Exception as e:
        Actor.log.debug(f"No pagination found: {str(e)}")

def extract_category_from_url(url):
    """Extract category from URL"""
    if '/cars' in url:
        return 'cars'
    elif '/property-for-sale' in url:
        return 'property-for-sale'
    elif '/property-to-rent' in url:
        return 'property-to-rent'
    elif '/jobs' in url:
        return 'jobs'
    elif '/farming' in url:
        return 'farming'
    elif '/services' in url:
        return 'services'
    return 'other'

if __name__ == '__main__':
    asyncio.run(main())
