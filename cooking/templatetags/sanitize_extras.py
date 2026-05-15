from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


ALLOWED_TAGS = {
    'a', 'b', 'blockquote', 'br', 'div', 'em', 'h3', 'h4', 'i', 'li',
    'ol', 'p', 'span', 'strong', 's', 'u', 'ul',
}
VOID_TAGS = {'br'}
ALLOWED_PROTOCOLS = {'', 'http', 'https', 'mailto'}


class RecipeHTMLSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            return

        if tag == 'a':
            href = ''
            for name, value in attrs:
                if name.lower() == 'href':
                    href = value or ''
                    break
            parsed_href = urlparse(href)
            if parsed_href.scheme not in ALLOWED_PROTOCOLS:
                self.output.append('<a rel="nofollow noopener">')
                return
            self.output.append(
                f'<a href="{escape(href, quote=True)}" rel="nofollow noopener" target="_blank">'
            )
            return

        self.output.append(f'<{tag}>')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.output.append(f'</{tag}>')

    def handle_data(self, data):
        self.output.append(escape(data))

    def handle_entityref(self, name):
        self.output.append(f'&{escape(name)};')

    def handle_charref(self, name):
        self.output.append(f'&#{escape(name)};')

    def get_html(self):
        return ''.join(self.output)


@register.filter
def safe_recipe_html(value):
    sanitizer = RecipeHTMLSanitizer()
    sanitizer.feed(value or '')
    sanitizer.close()
    return mark_safe(sanitizer.get_html())
