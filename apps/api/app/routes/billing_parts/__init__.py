"""Domain-specific billing route implementations.

The public route module keeps the compatibility surface and shared helpers.
Domain modules are deliberately independent from it at import time; the
facade-owned runtime provider keeps legacy monkeypatches observable at request
time without reversing the dependency direction.
"""
