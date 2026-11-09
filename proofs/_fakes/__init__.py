"""Fake endpoints the proofs start themselves (QA). Standard library only; nothing from the product.

Files here are not proofs (the runner skips names that start with an underscore). Each fake makes its own
certificates and media at run time under the folder it is given (`ctx.scratch`); nothing here is committed
that could be a key, a certificate or a media file.
"""
