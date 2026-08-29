"""Built-in deterministic software target packs.

Each pack is its own module -- ``richbuild.target_packs.nextjs`` today -- and is
imported from there. This package re-exported the Next.js names for a while and
nothing imported them through it, so it no longer pretends to be a second path.
The ``TargetPack`` protocol every pack will conform to lands with the second pack.
"""
