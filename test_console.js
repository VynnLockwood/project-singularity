const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  
  const errors = [];
  page.on('console', msg => {
    if (msg.type() === 'error') {
      errors.push(msg.text());
    }
  });
  
  page.on('pageerror', error => {
    errors.push(error.message);
  });
  
  try {
    await page.goto('https://singularity.lunariankids.com/', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(3000);
    
    console.log('=== CONSOLE ERRORS ===');
    if (errors.length === 0) {
      console.log('No errors found!');
    } else {
      errors.forEach(e => console.log('ERROR:', e));
    }
    
    const title = await page.title();
    console.log('Page title:', title);
    
    const bodyText = await page.locator('body').innerText();
    console.log('Body text preview:', bodyText.substring(0, 200));
    
  } catch (e) {
    console.log('Navigation error:', e.message);
  }
  
  await browser.close();
})();
