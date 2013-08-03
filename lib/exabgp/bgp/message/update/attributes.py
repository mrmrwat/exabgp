# encoding: utf-8
"""
set.py

Created by Thomas Mangin on 2010-01-16.
Copyright (c) 2009-2013  Exa Networks. All rights reserved.
"""

from struct import unpack,error

from exabgp.util.od import od
from exabgp.configuration.environment import environment
from exabgp.util.cache import Cache

from exabgp.protocol.family import AFI,SAFI

from exabgp.bgp.message.open.asn import ASN,AS_TRANS
from exabgp.bgp.message.notification import Notify
from exabgp.bgp.message.update.nlri.eor import RouteEOR
from exabgp.bgp.message.update.attribute.id import AttributeID as AID
from exabgp.bgp.message.update.attribute.flag import Flag

from exabgp.bgp.message.update.attribute.origin import Origin
from exabgp.bgp.message.update.attribute.aspath import ASPath,AS4Path
from exabgp.bgp.message.update.attribute.nexthop import cachedNextHop
from exabgp.bgp.message.update.attribute.med import MED
from exabgp.bgp.message.update.attribute.localpref import LocalPreference
from exabgp.bgp.message.update.attribute.atomicaggregate import AtomicAggregate
from exabgp.bgp.message.update.attribute.aggregator import Aggregator
from exabgp.bgp.message.update.attribute.communities import cachedCommunity,Communities,ECommunity,ECommunities
from exabgp.bgp.message.update.attribute.originatorid import OriginatorID
from exabgp.bgp.message.update.attribute.clusterlist import ClusterList

from exabgp.bgp.message.update.attribute.unknown import Unknown

from exabgp.logger import Logger,LazyFormat

# =================================================================== Attributes

class MultiAttributes (list):
	def __init__ (self,attribute):
		list.__init__(self)
		self.ID = attribute.ID
		self.FLAG = attribute.FLAG
		self.MULTIPLE = True
		self.append(attribute)

	def pack (self):
		r = []
		for attribute in self:
			r.append(attribute.pack())
		return ''.join(r)

	def __len__ (self):
		return len(self.pack())

	def __str__ (self):
		return 'MultiAttibutes(%s)' % ' '.join(str(_) for _ in self)

class Attributes (dict):
	routeFactory = None
	autocomplete = True
	cache = {}

	lookup = {
		AID.ORIGIN             : Origin,              # 1
		AID.AS_PATH            : ASPath,              # 2
		# NextHop                                     # 3
		AID.MED                : MED,                 # 4
		AID.LOCAL_PREF         : LocalPreference,     # 5
		AID.ATOMIC_AGGREGATE   : AtomicAggregate,     # 6
		AID.AGGREGATOR         : Aggregator,          # 7
		AID.COMMUNITY          : Communities,         # 8
		AID.ORIGINATOR_ID      : OriginatorID,        # 9
		AID.CLUSTER_LIST       : ClusterList,         # 10
		AID.EXTENDED_COMMUNITY : ECommunities,        # 16
		AID.AS4_PATH           : AS4Path,             # 17
		AID.AS4_AGGREGATOR     : Aggregator,          # 18
	}

	representation = {
		#	key:  (how, default, name, presentation),
		AID.ORIGIN             : ('string',  '', 'origin', '%s'),
		AID.AS_PATH            : ('list',    '', 'as-path', '%s'),
		AID.NEXT_HOP           : ('string',  '', 'next-hop', '%s'),
		AID.MED                : ('integer', '', 'med', '%s'),
		AID.LOCAL_PREF         : ('integer', '', 'local-preference', '%s'),
		AID.ATOMIC_AGGREGATE   : ('boolean', '', 'atomic-aggregate', '%s'),
		AID.AGGREGATOR         : ('string',  '', 'aggregator', '( %s )'),
		AID.COMMUNITY          : ('list',    '', 'community', '%s'),
		AID.ORIGINATOR_ID      : ('inet',    '', 'originator-id', '%s'),
		AID.CLUSTER_LIST       : ('list',    '', 'cluster-list', '%s'),
		AID.EXTENDED_COMMUNITY : ('list',    '', 'extended-community', '%s'),
	}

	# STRING = [_ for _ in representation if representation[_][0] == 'string']
	# INTEGER = [_ for _ in representation if representation[_][0] == 'integer']
	# LIST = [_ for _ in representation if representation[_][0] == 'list']
	# BOOLEAN = [_ for _ in representation if representation[_][0] == 'boolean']

	def __init__ (self):
		self._str = ''
		self._json = ''
		self.cache_attributes = environment.settings().cache.attributes
		self.cacheable = True
		self.seennlri = False

	def has (self,k):
		return k in self

	def add_from_cache (self,attributeid,data):
		if data in self.cache.setdefault(attributeid,Cache()):
			self.add(self.cache[attributeid].retrieve(data))
			return True
		return False

	def add (self,attribute,data=None):
		self._str = ''
		self._json = ''
		if data and self.cache_attributes:
			self.cache[attribute.ID].cache(data,attribute)
		if attribute.MULTIPLE:
			if self.has(attribute.ID):
				self[attribute.ID].append(attribute)
			else:
				self[attribute.ID] = MultiAttributes(attribute)
		else:
			self[attribute.ID] = attribute

	def remove (self,attrid):
		self.pop(attrid)

	def _as_path (self,asn4,asp):
		# if the peer does not understand ASN4, we need to build a transitive AS4_PATH
		if asn4:
			return asp.pack(True)

		as2_seq = [_ if not _.asn4() else AS_TRANS for _ in asp.as_seq]
		as2_set = [_ if not _.asn4() else AS_TRANS for _ in asp.as_set]

		message = ASPath(as2_seq,as2_set).pack(False)
		if AS_TRANS in as2_seq or AS_TRANS in as2_set:
			message += AS4Path(asp.as_seq,asp.as_set).pack()
		return message

	def pack (self,asn4,local_asn,peer_asn):
		ibgp = (local_asn == peer_asn)
		# we do not store or send MED
		message = ''

		if AID.ORIGIN in self:
			message += self[AID.ORIGIN].pack()
		elif self.autocomplete:
			message += Origin(Origin.IGP).pack()

		if AID.AS_PATH in self:
			asp = self[AID.AS_PATH]
			message += self._as_path(asn4,asp)
		elif self.autocomplete:
			if ibgp:
				asp = ASPath([],[])
			else:
				asp = ASPath([local_asn,],[])
			message += self._as_path(asn4,asp)
		else:
			raise RuntimeError('Generated routes must always have an AS_PATH ')

		if AID.NEXT_HOP in self:
			afi = self[AID.NEXT_HOP].afi
			safi = self[AID.NEXT_HOP].safi
			if afi == AFI.ipv4 and safi in [SAFI.unicast, SAFI.multicast]:
				message += self[AID.NEXT_HOP].pack()

		if AID.MED in self:
			message += self[AID.MED].pack()

		if ibgp:
			if AID.LOCAL_PREF in self:
				message += self[AID.LOCAL_PREF].pack()
			else:
				# '\x00\x00\x00d' is 100 packed in long network bytes order
				message += LocalPreference('\x00\x00\x00d').pack()

		# This generate both AGGREGATOR and AS4_AGGREGATOR
		if AID.AGGREGATOR in self:
			aggregator = self[AID.AGGREGATOR]
			message += aggregator.pack(asn4)

		for attribute in [
			AID.ATOMIC_AGGREGATE,
			AID.COMMUNITY,
			AID.ORIGINATOR_ID,
			AID.CLUSTER_LIST,
			AID.EXTENDED_COMMUNITY
		]:
			if attribute in self:
				message += self[attribute].pack()

		for attribute in self:
			if attribute in [AID.ORIGIN, AID.AS_PATH, AID.NEXT_HOP, AID.MED, AID.LOCAL_PREF,AID.AGGREGATOR, AID.ATOMIC_AGGREGATE, AID.COMMUNITY, AID.ORIGINATOR_ID, AID.CLUSTER_LIST, AID.EXTENDED_COMMUNITY]:
				pass
			message += self[attribute].pack()

		return message

	def json (self):
		if not self._json:
			def generate (self):
				for code in sorted(self.keys() + [AID.ATOMIC_AGGREGATE,]):
					if code in (AID.INTERNAL_SPLIT, AID.INTERNAL_WATCHDOG, AID.INTERNAL_WITHDRAW):
						continue
					if code in self.representation:
						how, default, name, presentation = self.representation[code]
						if how == 'boolean':
							yield '"%s": %s' % (name, 'true' if self.has(code) else 'false')
						elif how == 'string':
							yield '"%s": "%s"' % (name, presentation % str(self[code]))
						else:
							yield '"%s": %s' % (name, presentation % str(self[code]))
					else:
						yield '"attribute-0x%02X-0x%02X": %s' % (code,self[code].FLAG,str(self[code]))
			self._json = ', '.join(generate(self))
		return self._json

	def __str__ (self):
		if not self._str:
			def generate (self):
				for code in sorted(self.keys()):
					# XXX: FIXME: really we should have a INTERNAL attribute in the classes
					if code in (AID.INTERNAL_SPLIT, AID.INTERNAL_WATCHDOG, AID.INTERNAL_WITHDRAW):
						continue
					if code in self.representation:
						how, default, name, presentation = self.representation[code]
						if how == 'boolean':
							yield name
						else:
							yield '%s %s' % (name, presentation % str(self[code]))
					else:
						yield "attribute [ 0x%02X 0x%02X %s ]" % (code,self[code].FLAG,str(self[code]))
			# XXX: FIXME: remove this ' ' + ? should it be done by the caller ?
			self._str = ' ' + ' '.join(generate(self))
		return self._str

	def factory (self,data):
		if not data:
			return self

		# We do not care if the attribute are transitive or not as we do not redistribute
		flag = Flag(ord(data[0]))
		code = AID(ord(data[1]))

		if flag & Flag.EXTENDED_LENGTH:
			length = unpack('!H',data[2:4])[0]
			offset = 4
		else:
			length = ord(data[2])
			offset = 3

		data = data[offset:]
		next = data[length:]
		attribute = data[:length]

		logger = Logger()
		logger.parser(LazyFormat("parsing flag %x type %02x (%s) len %02x %s" % (flag,int(code),code,length,'payload ' if length else ''),od,data[:length]))

		if self.seennlri and code not in (AID.MP_REACH_NLRI, AID.MP_UNREACH_NLRI):
			self.cacheable = False

		if code == AID.ORIGIN:
			# This if block should never be called anymore ...
			if not self.add_from_cache(code,attribute):
				self.add(Origin(ord(attribute)),attribute)
			return self.factory(next)

		# only 2-4% of duplicated data - is it worth to cache ?
		if code == AID.AS_PATH:
			if length:
				# we store the AS4_PATH as AS_PATH, do not over-write
				if not self.has(code):
					if not self.add_from_cache(code,attribute):
						self.add(self.__new_ASPath(attribute),attribute)
			return self.factory(next)

		if code == AID.AS4_PATH:
			if length:
				# ignore the AS4_PATH on new spekers as required by RFC 4893 section 4.1
				if not self.negotiated.asn4:
					# This replace the old AS_PATH
					if not self.add_from_cache(code,attribute):
						self.add(self.__new_ASPath4(attribute),attribute)
			return self.factory(next)

		if code == AID.NEXT_HOP:
			if not self.add_from_cache(code,attribute):
				self.add(cachedNextHop(AFI.ipv4,SAFI.unicast_multicast,attribute),attribute)
			return self.factory(next)

		if code == AID.MED:
			if not self.add_from_cache(code,attribute):
				self.add(MED(attribute),attribute)
			return self.factory(next)

		if code == AID.LOCAL_PREF:
			if not self.add_from_cache(code,attribute):
				self.add(LocalPreference(attribute),attribute)
			return self.factory(next)

		if code == AID.ATOMIC_AGGREGATE:
			if not self.add_from_cache(code,attribute):
				raise Notify(3,2,'invalid ATOMIC_AGGREGATE %s' % [hex(ord(_)) for _ in attribute])
			return self.factory(next)

		if code == AID.AGGREGATOR:
			# AS4_AGGREGATOR are stored as AGGREGATOR - so do not overwrite if exists
			if not self.has(code):
				if not self.add_from_cache(AID.AGGREGATOR,attribute):
					self.add(Aggregator(attribute),attribute)
			return self.factory(next)

		if code == AID.AS4_AGGREGATOR:
			if not self.add_from_cache(AID.AGGREGATOR,attribute):
				self.add(Aggregator(attribute),attribute)
			return self.factory(next)

		if code == AID.COMMUNITY:
			if not self.add_from_cache(code,attribute):
				self.add(self.__new_communities(attribute),attribute)
			return self.factory(next)

		if code == AID.ORIGINATOR_ID:
			if not self.add_from_cache(code,attribute):
				self.add(OriginatorID(AFI.ipv4,SAFI.unicast,data[:4]),attribute)
			return self.factory(next)

		if code == AID.CLUSTER_LIST:
			if not self.add_from_cache(code,attribute):
				self.add(ClusterList(attribute),attribute)
			return self.factory(next)

		if code == AID.EXTENDED_COMMUNITY:
			if not self.add_from_cache(code,attribute):
				self.add(self.__new_extended_communities(attribute),attribute)
			return self.factory(next)

		if code == AID.MP_UNREACH_NLRI:
			self.seennlri = True

			# -- Reading AFI/SAFI
			data = data[:length]
			afi,safi = unpack('!HB',data[:3])
			offset = 3
			data = data[offset:]

			if (afi,safi) not in self.negotiated.families:
				raise Notify(3,0,'presented a non-negotiated family %d/%d' % (afi,safi))

			# Is the peer going to send us some Path Information with the route (AddPath)
			addpath = self.negotiated.addpath.receive(afi,safi)

			# XXX: we do assume that it is an EOR. most likely harmless
			if not data:
				self.mp_withdraw.append(RouteEOR(afi,safi,'announced'))
				return self.factory(next)

			while data:
				route = self.routeFactory(afi,safi,data,addpath,'withdrawn')
				route.attributes = self
				self.mp_withdraw.append(route)
				data = data[len(route.nlri):]
			return self.factory(next)

		if code == AID.MP_REACH_NLRI:
			self.seennlri = True

			data = data[:length]
			# -- Reading AFI/SAFI
			afi,safi = unpack('!HB',data[:3])
			offset = 3

			# we do not want to accept unknown families
			if (afi,safi) not in self.negotiated.families:
				raise Notify(3,0,'presented a non-negotiated family %d/%d' % (afi,safi))

			# -- Reading length of next-hop
			len_nh = ord(data[offset])
			offset += 1

			rd = 0

			# check next-hope size
			if afi == AFI.ipv4:
				if safi in (SAFI.unicast,SAFI.multicast):
					if len_nh != 4:
						raise Notify(3,0,'invalid ipv4 unicast/multicast next-hop length %d expected 4' % len_nh)
				if safi in (SAFI.mpls_vpn,):
					if len_nh != 12:
						raise Notify(3,0,'invalid ipv4 mpls_vpn next-hop length %d expected 12' % len_nh)
					rd = 8
				size = 4
			elif afi == AFI.ipv6:
				if safi in (SAFI.unicast,):
					if len_nh not in (16,32):
						raise Notify(3,0,'invalid ipv6 unicast next-hop length %d expected 16 or 32' % len_nh)
				if safi in (SAFI.mpls_vpn,):
					if len_nh not in (24,40):
						raise Notify(3,0,'invalid ipv6 mpls_vpn next-hop length %d expected 24 or 40' % len_nh)
					rd = 8
				size = 16

			# -- Reading next-hop
			nh = data[offset+rd:offset+rd+size]

			# chech the RD is well zeo
			if rd and sum([int(ord(_)) for _ in data[offset:8]]) != 0:
				raise Notify(3,0,"MP_REACH_NLRI next-hop's route-distinguisher must be zero")

			offset += len_nh

			# Skip a reserved bit as somone had to bug us !
			reserved = ord(data[offset])
			offset += 1

			if reserved != 0:
				raise Notify(3,0,'the reserved bit of MP_REACH_NLRI is not zero')

			# Is the peer going to send us some Path Information with the route (AddPath)
			addpath = self.negotiated.addpath.receive(afi,safi)

			# Reading the NLRIs
			data = data[offset:]

			while data:
				route = self.routeFactory(afi,safi,data,addpath,'announced')
				if not route.attributes.add_from_cache(AID.NEXT_HOP,nh):
					route.attributes.add(cachedNextHop(afi,safi,nh),nh)
				self.mp_announce.append(route)
				data = data[len(route.nlri):]
			return self.factory(next)

		if flag & Flag.TRANSITIVE:
			if not self.add_from_cache(code,attribute):
				self.add(Unknown(code,flag,attribute),attribute)
			return self.factory(next)

		logger.parser('ignoring non-transitive attribute')
		return self.factory(next)

	def merge_attributes (self):
		as2path = self[AID.AS_PATH]
		as4path = self[AID.AS4_PATH]
		self.remove(AID.AS_PATH)
		self.remove(AID.AS4_PATH)

		# this key is unique as index length is a two header, plus a number of ASN of size 2 or 4
		# so adding the : make the length odd and unique
		key = "%s:%s" % (as2path.index, as4path.index)

		# found a cache copy
		if self.add_from_cache(AID.AS_PATH,key):
			return

		as_seq = []
		as_set = []

		len2 = len(as2path.as_seq)
		len4 = len(as4path.as_seq)

		# RFC 4893 section 4.2.3
		if len2 < len4:
			as_seq = as2path.as_seq
		else:
			as_seq = as2path.as_seq[:-len4]
			as_seq.extend(as4path.as_seq)

		len2 = len(as2path.as_set)
		len4 = len(as4path.as_set)

		if len2 < len4:
			as_set = as4path.as_set
		else:
			as_set = as2path.as_set[:-len4]
			as_set.extend(as4path.as_set)

		aspath = ASPath(as_seq,as_set)
		self.add(aspath,key)

	def __new_communities (self,data):
		communities = Communities()
		while data:
			if data and len(data) < 4:
				raise Notify(3,1,'could not decode community %s' % str([hex(ord(_)) for _ in data]))
			communities.add(cachedCommunity(data[:4]))
			data = data[4:]
		return communities

	def __new_extended_communities (self,data):
		communities = ECommunities()
		while data:
			if data and len(data) < 8:
				raise Notify(3,1,'could not decode extended community %s' % str([hex(ord(_)) for _ in data]))
			communities.add(ECommunity(data[:8]))
			data = data[8:]
		return communities

	def __new_aspaths (self,data,asn4,klass):
		as_set = []
		as_seq = []
		backup = data

		unpacker = {
			False : '!H',
			True  : '!L',
		}
		size = {
			False: 2,
			True : 4,
		}
		as_choice = {
			ASPath.AS_SEQUENCE : as_seq,
			ASPath.AS_SET      : as_set,
		}

		upr = unpacker[asn4]
		length = size[asn4]

		try:

			while data:
				stype = ord(data[0])
				slen  = ord(data[1])

				if stype not in (ASPath.AS_SET, ASPath.AS_SEQUENCE):
					raise Notify(3,11,'invalid AS Path type sent %d' % stype)

				end = 2+(slen*length)
				sdata = data[2:end]
				data = data[end:]
				asns = as_choice[stype]

				for i in range(slen):
					asn = unpack(upr,sdata[:length])[0]
					asns.append(ASN(asn))
					sdata = sdata[length:]

		except IndexError:
			raise Notify(3,11,'not enough data to decode AS_PATH or AS4_PATH')
		except error:  # struct
			raise Notify(3,11,'not enough data to decode AS_PATH or AS4_PATH')

		return klass(as_seq,as_set,backup)

	def __new_ASPath (self,data):
		return self.__new_aspaths(data,self.negotiated.asn4,ASPath)

	def __new_ASPath4 (self,data):
		return self.__new_aspaths(data,True,AS4Path)

if not Attributes.cache:
	for attribute in AID._str:
		Attributes.cache[attribute] = Cache()

	# There can only be one, build it now :)
	Attributes.cache[AID.ATOMIC_AGGREGATE][''] = AtomicAggregate()

	IGP = Origin(Origin.IGP)
	EGP = Origin(Origin.EGP)
	INC = Origin(Origin.INCOMPLETE)

	Attributes.cache[AID.ORIGIN][IGP.pack()] = IGP
	Attributes.cache[AID.ORIGIN][EGP.pack()] = EGP
	Attributes.cache[AID.ORIGIN][INC.pack()] = INC


def AttributesFactory (routefactory,negotiated,data):
	try:
		attributes = Attributes()
		attributes.routeFactory = routefactory
		# XXX: hackish for now
		attributes.mp_announce = []
		attributes.mp_withdraw = []

		attributes.negotiated = negotiated
		attributes.factory(data)
		if AID.AS_PATH in attributes and AID.AS4_PATH in attributes:
			attributes.merge_attributes()
		return attributes
	except IndexError:
		raise Notify(3,2,data)
